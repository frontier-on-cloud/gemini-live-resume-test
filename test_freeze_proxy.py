"""Offline check of freeze_proxy.py (no network beyond 127.0.0.1, no key).

1. A local websockets server sends a message every 100 ms. The client connects through
   the proxy (websockets `proxy=` argument, keepalive pings off), reads for 1 s, then the
   tunnel is frozen for 5 s: the client must receive nothing and see no close; what it
   sends must not reach the server; the websocket stays OPEN. After unfreeze, the
   backlog arrives both ways.
2. Freeze again, and the server closes its side during the freeze. The client must
   still see no close while frozen; the proxy's TCP probe must timestamp the server's
   FIN/RST; after unfreeze the client sees the close.

Run: uv run test_freeze_proxy.py
"""

import asyncio
import time

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from freeze_proxy import FreezeProxy

EVENTS: list[tuple[float, str, dict]] = []
T0 = time.monotonic()


def log(name, **fields):
    EVENTS.append((round(time.monotonic() - T0, 3), name, fields))


async def main() -> None:
    server_rx: list[tuple[float, str]] = []
    server_ws = {}
    close_now = asyncio.Event()

    async def handler(ws):
        server_ws["ws"] = ws

        async def reader():
            async for m in ws:
                server_rx.append((time.monotonic() - T0, m))

        rt = asyncio.create_task(reader())
        n = 0
        try:
            while not close_now.is_set():
                n += 1
                await ws.send(f"s{n}")
                await asyncio.sleep(0.1)
            await ws.close(4001, "server closing during freeze")
        except ConnectionClosed:
            pass
        rt.cancel()

    async with serve(handler, "127.0.0.1", 0, close_timeout=2) as srv:
        port = srv.sockets[0].getsockname()[1]
        proxy = await FreezeProxy({f"127.0.0.1:{port}"}, on_event=log).start()
        ws = await connect(f"ws://127.0.0.1:{port}/", proxy=proxy.url, ping_interval=None)
        tunnel = list(proxy.tunnels.values())[0]
        assert tunnel.client_port == ws.transport.get_extra_info("sockname")[1], "port map"

        async def recv_for(seconds):
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

        got, closed = await recv_for(1.0)
        print(f"before freeze: {len(got)} msgs, closed={closed}")
        assert len(got) >= 8 and closed is None

        # --- part 1: freeze with traffic both ways
        proxy.freeze(tunnel.id)
        rx_before = len(server_rx)
        for i in range(20):
            await asyncio.wait_for(ws.send(f"c{i}"), 1.0)
        got, closed = await recv_for(5.0)
        print(f"during freeze: client got {len(got)} msgs (a few may be in flight), "
              f"closed={closed}, state={ws.state.name}, server got {len(server_rx) - rx_before}")
        assert closed is None and ws.state.name == "OPEN"
        assert len(got) <= 2, "at most what was already in flight"
        assert len(server_rx) - rx_before == 0
        proxy.unfreeze(tunnel.id)
        got, closed = await recv_for(1.0)
        await asyncio.sleep(0.3)
        print(f"after unfreeze: client got {len(got)} msgs in 1 s, server got "
              f"{len(server_rx) - rx_before} of 20")
        assert len(got) >= 40 and len(server_rx) - rx_before == 20 and closed is None

        # --- part 2: server closes during a freeze
        proxy.freeze(tunnel.id)
        t_freeze = time.monotonic() - T0
        close_now.set()
        got, closed = await recv_for(5.0)
        print(f"server closed during freeze: client closed={closed}, state={ws.state.name}")
        assert closed is None and ws.state.name == "OPEN"
        changes = [e for e in EVENTS if e[1] == "tcp_state_change" and e[0] > t_freeze]
        for e in changes:
            print(f"  probe @{e[0] - t_freeze:.2f}s after freeze: {e[2]['before']} -> {e[2]['after']}")
        assert changes, "the probe should see the server-side FIN/RST while frozen"
        proxy.unfreeze(tunnel.id)
        got, closed = await recv_for(3.0)
        rcvd = getattr(closed, "rcvd", None)
        print(f"after unfreeze: client got {len(got)} msgs then close "
              f"code={getattr(rcvd, 'code', None)} reason={getattr(rcvd, 'reason', None)!r} "
              f"({type(closed).__name__})")
        assert closed is not None
        await proxy.stop()
    print("OK: no close seen by the client while frozen; probe saw the server's close")


if __name__ == "__main__":
    asyncio.run(main())
