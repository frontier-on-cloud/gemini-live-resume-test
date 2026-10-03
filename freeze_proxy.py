"""freeze_proxy: a local HTTP CONNECT proxy that can freeze a tunnel.

The client (websockets, via `proxy="http://127.0.0.1:<port>"`) opens a CONNECT tunnel
to the Live API host; the proxy copies the TLS bytes untouched in both directions.
`freeze(tunnel_id)` stops forwarding in both directions without closing either socket:
both transports stop reading (transport.pause_reading()) and nothing is written, so
data piles up in kernel buffers and then the TCP windows close. `unfreeze()` resumes
reading and forwarding, and data held during the freeze is delivered.

Not a perfect network loss: while frozen, the proxy's kernel still ACKs what fits in
its receive buffer and answers zero-window probes and TCP keepalives, so neither peer
sees a TCP-level failure. What both peers see is the application-level effect of a
loss: no bytes arrive, and their writes stall once buffers are full.

While a tunnel is frozen, a probe reads the macOS TCP_CONNECTION_INFO of both
sockets every 250 ms without consuming data: state (4 ESTABLISHED, 5 CLOSE_WAIT = the
peer sent FIN, 0 CLOSED = reset), bytes received by the kernel, bytes waiting in the
send buffer, advertised receive window. So a FIN or RST from the server during a
freeze is timestamped even though nothing is read.

Only CONNECT to an allow-listed host:port is accepted. The proxy never sees plaintext
(TLS runs end to end through the tunnel) and logs only byte counts and TCP state.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

TCP_CONNECTION_INFO = 0x106  # macOS <netinet/tcp.h>
_TCPI_HEAD = struct.Struct("<BBBxIIIIIIIIIIII")       # 56 bytes
_TCPI_TAIL = struct.Struct("<QQQQQQQ")                 # at offset 56
TCP_STATES = {0: "CLOSED", 1: "LISTEN", 2: "SYN_SENT", 3: "SYN_RECEIVED", 4: "ESTABLISHED",
              5: "CLOSE_WAIT", 6: "FIN_WAIT_1", 7: "CLOSING", 8: "LAST_ACK", 9: "FIN_WAIT_2",
              10: "TIME_WAIT"}


def tcp_info(sock: Any) -> dict | None:
    """macOS only; None elsewhere or on error."""
    if sys.platform != "darwin" or sock is None:
        return None
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, TCP_CONNECTION_INFO, 112)
        h = _TCPI_HEAD.unpack_from(raw, 0)
        t = _TCPI_TAIL.unpack_from(raw, 56)
    except Exception:
        return None
    return {"state": TCP_STATES.get(h[0], str(h[0])), "rto_ms": h[5], "snd_wnd": h[9],
            "snd_sbbytes": h[10], "rcv_wnd": h[11], "srtt_ms": h[13],
            "txbytes": t[1], "txretransmitbytes": t[2], "rxbytes": t[4]}


@dataclass
class Tunnel:
    id: int
    client_port: int
    target: str
    opened_s: float
    c_reader: asyncio.StreamReader
    c_writer: asyncio.StreamWriter
    s_reader: asyncio.StreamReader
    s_writer: asyncio.StreamWriter
    flowing: asyncio.Event = field(default_factory=asyncio.Event)
    frozen: bool = False
    bytes_c2s: int = 0
    bytes_s2c: int = 0
    held: dict = field(default_factory=lambda: {"c2s": 0, "s2c": 0})
    closed: dict = field(default_factory=dict)   # direction -> how ("eof" / "reset ...")
    tasks: list = field(default_factory=list)
    probe_task: asyncio.Task | None = None
    last_state: dict = field(default_factory=dict)

    def socks(self) -> tuple[Any, Any]:
        return (self.c_writer.get_extra_info("socket"), self.s_writer.get_extra_info("socket"))

    def probe(self) -> dict:
        c, s = self.socks()
        return {"client_side": tcp_info(c), "server_side": tcp_info(s)}


class FreezeProxy:
    def __init__(self, allowed: set[str], on_event: Callable[..., Any] | None = None) -> None:
        self.allowed = allowed
        self.on_event = on_event
        self.tunnels: dict[int, Tunnel] = {}
        self._n = 0
        self.server: asyncio.base_events.Server | None = None
        self.port: int | None = None

    def emit(self, name: str, **fields: Any) -> None:
        if self.on_event is not None:
            self.on_event(name, **fields)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> "FreezeProxy":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for t in list(self.tunnels.values()):
            self.close_tunnel(t.id)
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    def tunnel_for_client_port(self, port: int) -> Tunnel | None:
        return next((t for t in self.tunnels.values() if t.client_port == port), None)

    # ------------------------------------------------------------- connect --
    async def _handle(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        peer = c_writer.get_extra_info("peername") or ("?", 0)
        try:
            head = await asyncio.wait_for(c_reader.readuntil(b"\r\n\r\n"), 10)
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method, target, _ = line.split(" ", 2)
            if method != "CONNECT" or target not in self.allowed:
                c_writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await c_writer.drain()
                c_writer.close()
                self.emit("refused", request=line[:120])
                return
            host, port = target.rsplit(":", 1)
            s_reader, s_writer = await asyncio.open_connection(host, int(port))
        except Exception as exc:
            self.emit("connect_failed", detail=f"{type(exc).__name__}: {exc}"[:200])
            c_writer.close()
            return
        c_writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await c_writer.drain()
        self._n += 1
        t = Tunnel(self._n, peer[1], target, time.monotonic(), c_reader, c_writer, s_reader,
                   s_writer)
        t.flowing.set()
        self.tunnels[t.id] = t
        self.emit("tunnel_open", tunnel=t.id, client_port=t.client_port, target=target)
        t.tasks = [asyncio.create_task(self._pump(t, "c2s", c_reader, s_writer, c_writer)),
                   asyncio.create_task(self._pump(t, "s2c", s_reader, c_writer, s_writer))]

    async def _pump(self, t: Tunnel, d: str, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter, src_writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await t.flowing.wait()
                data = await reader.read(65536)
                if not data:
                    t.closed[d] = "eof"
                    self.emit("eof", tunnel=t.id, direction=d, frozen=t.frozen,
                              tcp=t.probe())
                    await t.flowing.wait()   # never propagate a close while frozen
                    if writer.can_write_eof():
                        writer.write_eof()
                    else:
                        writer.close()
                    return
                if t.frozen:
                    t.held[d] += len(data)
                await t.flowing.wait()       # bytes read just before a freeze wait here
                writer.write(data)
                await writer.drain()
                if d == "c2s":
                    t.bytes_c2s += len(data)
                else:
                    t.bytes_s2c += len(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            t.closed[d] = f"{type(exc).__name__}: {exc}"[:160]
            self.emit("pump_error", tunnel=t.id, direction=d, frozen=t.frozen,
                      detail=t.closed[d], tcp=t.probe())
            await t.flowing.wait()
            # Propagate an abortive close to the other side, as a real reset would.
            for w in (writer, src_writer):
                try:
                    w.transport.abort()
                except Exception:
                    pass

    # -------------------------------------------------------------- control --
    def freeze(self, tunnel_id: int) -> dict:
        t = self.tunnels[tunnel_id]
        t.frozen = True
        t.flowing.clear()
        for w in (t.c_writer, t.s_writer):
            w.transport.pause_reading()
        snap = t.probe()
        t.last_state = {k: (v or {}).get("state") for k, v in snap.items()}
        t.probe_task = asyncio.create_task(self._probe_loop(t))
        self.emit("freeze", tunnel=t.id, bytes_c2s=t.bytes_c2s, bytes_s2c=t.bytes_s2c, tcp=snap)
        return snap

    def unfreeze(self, tunnel_id: int) -> dict:
        t = self.tunnels[tunnel_id]
        snap = t.probe()
        if t.probe_task is not None:
            t.probe_task.cancel()
        t.frozen = False
        for w in (t.c_writer, t.s_writer):
            try:
                w.transport.resume_reading()
            except Exception:
                pass
        t.flowing.set()
        self.emit("unfreeze", tunnel=t.id, bytes_c2s=t.bytes_c2s, bytes_s2c=t.bytes_s2c,
                  held=dict(t.held), tcp=snap)
        return snap

    async def _probe_loop(self, t: Tunnel) -> None:
        last_snapshot = time.monotonic()
        while t.frozen:
            await asyncio.sleep(0.25)
            for w in (t.c_writer, t.s_writer):   # stay paused even if a reader resumed it
                try:
                    w.transport.pause_reading()
                except Exception:
                    pass
            snap = t.probe()
            state = {k: (v or {}).get("state") for k, v in snap.items()}
            if state != t.last_state:
                self.emit("tcp_state_change", tunnel=t.id, before=t.last_state, after=state,
                          tcp=snap)
                t.last_state = state
            if time.monotonic() - last_snapshot >= 1.0:
                last_snapshot = time.monotonic()
                self.emit("frozen_probe", tunnel=t.id, tcp=snap)

    def close_tunnel(self, tunnel_id: int) -> None:
        t = self.tunnels.get(tunnel_id)
        if t is None:
            return
        t.frozen = False
        if t.probe_task is not None:
            t.probe_task.cancel()
        for task in t.tasks:
            task.cancel()
        for w in (t.c_writer, t.s_writer):
            try:
                w.transport.abort()
            except Exception:
                pass
        self.tunnels.pop(tunnel_id, None)
