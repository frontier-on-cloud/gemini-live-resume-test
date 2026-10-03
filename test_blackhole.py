"""Offline check of blackhole.py: two containers on the Docker bridge, no key, no
Internet.

  docker run -d --rm --name bh-server r3-blackhole python test_blackhole.py server
  docker run --rm --cap-add NET_ADMIN r3-blackhole python test_blackhole.py client \
      $(docker inspect -f '{{.NetworkSettings.IPAddress}}' bh-server)

The server (its own network namespace, no capabilities) sends a message every 100 ms
on /data and logs what it receives and the TCP state of its socket (ss, every 250 ms).
/ctl returns that log, over a separate connection (another 4-tuple, so it is not
blackholed). The client (NET_ADMIN) checks:

1. Blackhole the /data flow for 6 s while both sides keep sending. The client must get
   nothing and see no close; the server must get none of the client's 20 messages and
   see no close; neither socket may leave ESTABLISHED; tcpdump on the client's eth0 must
   show server segments arriving and no client segment leaving; the RST and FIN
   counters must stay at 0. Remove the rule: the backlog must arrive both ways.
2. Blackhole again, and the server closes during it: a close frame, then 1 s later a
   reset (SO_LINGER 0 and abort). The client must still see no close; the RST counter
   must count the server's reset. Remove the rule and send one message: it meets the
   server's dead socket, and the client sees the connection end.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import sys
import time

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from blackhole import Blackhole, FlowWatch, ss_flow

PORT = 8765


# -------------------------------------------------------------------- server --

async def server_main() -> None:
    log: list[dict] = []
    close_now = asyncio.Event()

    def ev(name: str, **f) -> None:
        log.append({"t": round(time.monotonic(), 3), "ev": name} | f)

    async def data(ws) -> None:
        peer = ws.remote_address
        local = ws.local_address
        ev("data_open", peer=list(peer))
        last_state = None

        async def reader() -> None:
            try:
                async for m in ws:
                    ev("rx", msg=m)
            except ConnectionClosed as exc:
                ev("rx_closed", detail=str(exc))

        async def prober() -> None:
            nonlocal last_state
            while True:
                s = ss_flow((local[0], local[1]), (peer[0], peer[1]))
                key = (s.get("state"), s.get("retrans"), s.get("backoff"))
                if key != last_state:
                    ev("tcp", **s)
                    last_state = key
                await asyncio.sleep(0.25)

        tasks = [asyncio.create_task(reader()), asyncio.create_task(prober())]
        n = 0
        try:
            while not close_now.is_set():
                n += 1
                await ws.send(f"s{n}")
                await asyncio.sleep(0.1)
            # Close frame first (data), then after 1 s a reset (SO_LINGER 0 + abort),
            # so that both a data segment and a RST are sent into the blackhole.
            ev("server_close_frame")
            closer = asyncio.create_task(ws.close(4001, "server closing during blackhole"))
            await asyncio.sleep(1.0)
            sock = ws.transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            ws.transport.abort()
            ev("server_reset_sent")
            await asyncio.gather(closer, return_exceptions=True)
        except ConnectionClosed as exc:
            ev("send_closed", detail=str(exc))
        await asyncio.sleep(1.0)
        for t in tasks:
            t.cancel()

    async def handler(ws) -> None:
        if ws.request.path == "/ctl":
            async for m in ws:
                if m == "close_data":
                    close_now.set()
                    await ws.send("ok")
                elif m == "log":
                    await ws.send(json.dumps(log))
            return
        await data(ws)

    async with serve(handler, "0.0.0.0", PORT, close_timeout=2, ping_interval=None):
        print(f"server listening on {PORT}", flush=True)
        await asyncio.Future()


# -------------------------------------------------------------------- client --

async def client_main(host: str) -> None:
    T0 = time.monotonic()
    pkts: list[dict] = []
    ws = await connect(f"ws://{host}:{PORT}/data", ping_interval=None, close_timeout=2)
    local = ws.transport.get_extra_info("sockname")[:2]
    remote = ws.transport.get_extra_info("peername")[:2]
    bh = Blackhole()
    watch = FlowWatch(local, remote, "eth0", pkts.append)   # p["ts"]: capture time
    print(f"flow {local} -> {remote}; {bh.version}; tcpdump: {await watch.start()}")

    async def ctl(cmd: str) -> str:
        async with connect(f"ws://{host}:{PORT}/ctl", ping_interval=None) as c:
            await c.send(cmd)
            return await c.recv()

    async def recv_for(seconds: float):
        got, closed = [], None
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                got.append(await asyncio.wait_for(ws.recv(), end - time.monotonic()))
            except TimeoutError:
                break
            except ConnectionClosed as exc:
                closed = exc
                break
        return got, closed

    def window(t_a: float, t_b: float) -> dict:   # wall-clock capture times
        sel = [p for p in pkts if t_a <= p["ts"] <= t_b]
        return {"s2c": sum(p["dir"] == "s2c" for p in sel),
                "c2s": sum(p["dir"] == "c2s" for p in sel),
                "s2c_flags": sorted({p["flags"] for p in sel if p["dir"] == "s2c"})}

    got, closed = await recv_for(1.0)
    print(f"before blackhole: {len(got)} msgs, closed={closed}")
    assert len(got) >= 8 and closed is None

    # ---- part 1
    srv_rx_before = sum(e["ev"] == "rx" for e in json.loads(await ctl("log")))
    await recv_for(0.3)   # consume what is already buffered
    info = bh.add(local, remote)
    t_bh, w_bh = info["t_mono"], info["t_wall"]
    print("rules:", *info["rules"], sep="\n  ")
    for i in range(20):
        await asyncio.wait_for(ws.send(f"c{i}"), 1.0)
    got, closed = await recv_for(6.0)
    w_end = time.time()
    cnt = bh.counters()
    ss_c = ss_flow(local, remote)
    srv = json.loads(await ctl("log"))
    srv_rx = [e for e in srv if e["ev"] == "rx"]
    srv_tcp = [e for e in srv if e["ev"] == "tcp" and e["t"] >= t_bh]
    srv_closed = [e for e in srv if e["ev"] in ("rx_closed", "send_closed")]
    w = window(w_bh, w_end)
    print(f"during 6 s blackhole: client got {len(got)} msgs, closed={closed}, "
          f"state={ws.state.name}; server got {len(srv_rx) - srv_rx_before} of 20, "
          f"server saw close: {bool(srv_closed)}")
    print(f"  wire (client eth0): {w}")
    print(f"  counters: {cnt}")
    print(f"  client socket: {ss_c}")
    print(f"  server socket: " + "; ".join(
        f"+{e['t'] - t_bh:.2f}s {e.get('state')} retrans={e.get('retrans')} "
        f"backoff={e.get('backoff')}" for e in srv_tcp))
    # at most what was already in the client's socket before the rule took effect
    assert closed is None and ws.state.name == "OPEN" and len(got) <= 2
    assert len(srv_rx) - srv_rx_before == 0 and not srv_closed
    assert cnt["in"]["all"][0] == w["s2c"], "every server segment seen was dropped"
    assert w["c2s"] == 0, "a client segment left despite the rule"
    assert w["s2c"] > 0, "server segments should keep arriving (and be dropped)"
    assert cnt["in"]["rst"][0] == 0 and cnt["in"]["fin"][0] == 0
    assert cnt["out"]["rst"][0] == 0 and cnt["out"]["fin"][0] == 0
    assert cnt["out"]["all"][0] > 0 and cnt["in"]["all"][0] > 0
    assert ss_c["state"] == "ESTAB" and all(e.get("state") == "ESTAB" for e in srv_tcp)
    assert any(e.get("retrans") not in (None, "0/0") for e in srv_tcp), \
        "the server should be retransmitting"
    rm = bh.remove()
    got, closed = await recv_for(15.0)
    await asyncio.sleep(0.5)
    srv = json.loads(await ctl("log"))
    srv_rx = [e for e in srv if e["ev"] == "rx"]
    first_rx = min((e["t"] for e in srv_rx[srv_rx_before:]), default=None)
    print(f"after removing the rule: client got {len(got)} msgs in 15 s, server got "
          f"{len(srv_rx) - srv_rx_before} of 20 (first +{(first_rx or 0) - rm['t_mono']:.2f} s "
          f"after removal), closed={closed}")
    assert len(got) >= 60 and len(srv_rx) - srv_rx_before == 20 and closed is None

    # ---- part 2: the server closes during a blackhole
    await recv_for(0.3)
    info = bh.add(local, remote)
    t_bh, w_bh = info["t_mono"], info["t_wall"]
    await ctl("close_data")
    got, closed = await recv_for(8.0)
    cnt = bh.counters()
    w = window(w_bh, time.time())
    srv = json.loads(await ctl("log"))
    srv_tcp = [e for e in srv if e["ev"] == "tcp" and e["t"] >= t_bh]
    print(f"server closes during blackhole: client closed={closed}, state={ws.state.name}, "
          f"msgs={len(got)}")
    print(f"  wire (client eth0): {w}")
    print(f"  counters: {cnt}")
    print(f"  server socket: " + "; ".join(
        f"+{e['t'] - t_bh:.2f}s {e.get('state')} retrans={e.get('retrans')}" for e in srv_tcp))
    assert closed is None and ws.state.name == "OPEN" and len(got) <= 2
    assert w["c2s"] == 0
    assert cnt["in"]["rst"][0] > 0, "the server's RST should be counted"
    assert "R" in "".join(w["s2c_flags"]), "the RST should be visible on the wire"
    rm = bh.remove()
    try:
        await asyncio.wait_for(ws.send("probe after removal"), 1.0)
    except ConnectionClosed:
        pass
    got, closed = await recv_for(30.0)
    rcvd = getattr(closed, "rcvd", None)
    t_c = time.monotonic() - rm["t_mono"]
    print(f"after removing the rule: client got {len(got)} msgs, then "
          f"{type(closed).__name__} code={getattr(rcvd, 'code', None)} "
          f"reason={getattr(rcvd, 'reason', None)!r} +{t_c:.2f} s after removal")
    await asyncio.sleep(0.5)   # let the tcpdump reader catch up
    after = [p for p in pkts if p["ts"] >= rm["t_wall"]]
    print(f"  wire after removal: " + ", ".join(f"{p['dir']} [{p['flags']}]" for p in after[:12]))
    assert closed is not None
    bh.cleanup()
    await watch.stop()
    print(f"OK ({time.monotonic() - T0:.1f} s): nothing crossed the blackhole either way, "
          f"no close on either side while it held; the server's reset was counted and dropped")


if __name__ == "__main__":
    if sys.argv[1:2] == ["server"]:
        asyncio.run(server_main())
    else:
        asyncio.run(client_main(sys.argv[2]))
