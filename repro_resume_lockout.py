"""Gemini Live API: after a silent connection loss, session resumption is refused with 1011
until the server releases the old connection. A session with session resumption on is
opened through a tiny local HTTP CONNECT proxy (below). After one text turn the proxy
freezes the tunnel: nothing is forwarded either way and no socket is closed, so the server
sees no close frame, no FIN and no RST, as when a phone drops off Wi-Fi. The client then
resumes with the handle from the first SessionResumptionUpdate every 5 s for --seconds, on
direct connections. Control: the tunnel is unfrozen, the client closes the first connection
(WebSocket close 1000, which now reaches the server), and one more resume is tried.
Run: GEMINI_API_KEY=... uv run repro_resume_lockout.py [--seconds 60] (or the key in .env
here). No Docker, no root; run on macOS. gemini-3.8-live, Gemini Developer API, google-genai
2.25.0, AUDIO replies with output transcription. The handle is never printed. 2026-10-03."""
import argparse, asyncio, os, re, sys, time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, live, types

MODEL, HOST, PORT = "gemini-3.8-live", "generativelanguage.googleapis.com", 443
SYSTEM = "You are a helpful assistant. Answer in one short sentence."
QUOTA = re.compile(r"quota|billing|RESOURCE_EXHAUSTED|exceeded|\b429\b", re.IGNORECASE)


class FreezeProxy:
    """CONNECT proxy for HOST:PORT only; copies TLS bytes untouched (no plaintext, no key)."""

    def __init__(self) -> None:
        self.flowing, self.transports, self.tasks = asyncio.Event(), [], []
        self.flowing.set()

    async def start(self) -> str:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def handle(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        request = (await c_reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0].decode()
        if request.split(" ")[:2] != ["CONNECT", f"{HOST}:{PORT}"]:
            return c_writer.close()
        s_reader, s_writer = await asyncio.open_connection(HOST, PORT)
        c_writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self.transports += [c_writer.transport, s_writer.transport]
        self.tasks += [asyncio.create_task(self.pump(c_reader, s_writer)),
                       asyncio.create_task(self.pump(s_reader, c_writer))]

    async def pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                await self.flowing.wait()   # bytes read just before a freeze wait here
                writer.write(data)
                await writer.drain()
            await self.flowing.wait()       # never pass a close on while frozen
            writer.close()
        except OSError:
            pass

    def freeze(self, on: bool) -> None:
        """Stop (or resume) forwarding in both directions. No socket is closed."""
        self.flowing.clear() if on else self.flowing.set()
        for t in self.transports:
            t.pause_reading() if on else t.resume_reading()


# google-genai opens its websocket with `live.ws_connect` (websockets.asyncio.client.connect)
# and passes no proxy argument. Replacing that module attribute routes only the FIRST
# connection through the proxy, with websockets' keepalive pings off (otherwise websockets
# itself would give up on the frozen socket after 20 to 40 s). Every later connection (the
# resume attempts) goes direct: proxy=None also ignores any system or environment proxy.
_ws_connect, FIRST = live.ws_connect, []   # FIRST holds the proxy URL until it is used


def ws_connect(*args, **kwargs):
    proxy = FIRST.pop() if FIRST else None
    return _ws_connect(*args, **kwargs, proxy=proxy, **({"ping_interval": None} if proxy else {}))


live.ws_connect = ws_connect


def config(handle: str | None = None) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM)]),
        session_resumption=types.SessionResumptionConfig(handle=handle))


def clean(text: str) -> str:
    text = text.replace(k, "[REDACTED]") if (k := os.environ.get("GEMINI_API_KEY")) else text
    if QUOTA.search(text):
        print(f"stopping on a quota or billing error: {text}")
        sys.exit(3)
    return text


async def resume(client: genai.Client, handle: str) -> str:
    """One resume attempt with the handle; an accepted one is closed again (close 1000)."""
    t, cm = time.monotonic(), client.aio.live.connect(model=MODEL, config=config(handle))
    took = lambda: f"after {int((time.monotonic() - t) * 1000)} ms"
    try:
        await asyncio.wait_for(cm.__aenter__(), 10)
    except errors.APIError as e:   # the SDK turns the close of a refused setup into this
        return clean(f'refused: close {e.code} "{e.details}" {took()}')
    except TimeoutError:
        return "no setupComplete and no close within 10 s"
    except Exception as e:
        return clean(f"error {type(e).__name__}: {e} {took()}")
    await cm.__aexit__(None, None, None)
    return f"setupComplete {took()}"


async def main() -> None:
    p = argparse.ArgumentParser(description="Gemini Live: resume refused after a silent loss.")
    p.add_argument("--seconds", type=float, default=60.0, help="resume every 5 s for this long")
    a = p.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or sys.exit("no GEMINI_API_KEY"))
    t0, handle, said = time.monotonic(), [], []
    got_handle, turn_done = asyncio.Event(), asyncio.Event()
    log = lambda text: print(f"{int((time.monotonic() - t0) * 1000):>6} ms  {text}")
    since = lambda t: f"+{time.monotonic() - t:5.1f} s"
    print(f"model={MODEL}  google-genai {genai.__version__}  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    proxy = FreezeProxy()
    FIRST.append(await proxy.start())
    cm = client.aio.live.connect(model=MODEL, config=config())
    try:
        session = await cm.__aenter__()
    except Exception as e:
        sys.exit(clean(f"connection 1 failed: {type(e).__name__}: {e}"))
    log("connection 1 (through the local proxy): setupComplete")

    async def receive() -> None:
        while True:   # session.receive() returns after each turnComplete
            async for msg in session.receive():
                u, sc = msg.session_resumption_update, msg.server_content
                if u and u.new_handle and u.resumable and not handle:
                    handle.append(u.new_handle)
                    log(f"SessionResumptionUpdate: resumable=true, handle of {len(u.new_handle)} "
                        "chars (kept, not printed)")
                    got_handle.set()
                if sc and sc.output_transcription and sc.output_transcription.text:
                    said.append(sc.output_transcription.text)
                if sc and sc.turn_complete:
                    turn_done.set()

    rx = asyncio.create_task(receive())
    await asyncio.wait_for(got_handle.wait(), 15)
    await session.send_realtime_input(text="Hi, can you hear me?")
    log('sent text: "Hi, can you hear me?"')
    await asyncio.wait_for(turn_done.wait(), 30)
    log(f'model said: "{"".join(said).strip()}" (turnComplete)')
    await asyncio.sleep(1)
    proxy.freeze(True)
    t_f, outcomes = time.monotonic(), []
    log("tunnel frozen: nothing forwarded either way, no socket closed")
    for k in range(1, int(a.seconds // 5) + 1):
        await asyncio.sleep(max(0.0, t_f + 5 * k - time.monotonic()))
        start, out = since(t_f), await resume(client, handle[0])
        print(f"{start}  resume attempt {k} (connection 1 frozen): {out}")
        outcomes.append(out)
    rx.cancel()
    proxy.freeze(False)   # the client's close below now reaches the server
    await asyncio.wait_for(cm.__aexit__(None, None, None), 15)
    proto = session._ws.protocol   # the SDK's websockets connection (private attribute)
    print(f"{since(t_f)}  control: tunnel unfrozen, client closed connection 1 (close sent "
          f"{getattr(proto.close_sent, 'code', None)}, server echoed "
          f"{getattr(proto.close_rcvd, 'code', None)})")
    await asyncio.sleep(2)   # an immediate resume after an idle drop can fail even after a close
    start, control = since(t_f), await resume(client, handle[0])
    print(f"{start}  control resume (2 s after the close): {control}")
    refused = [o for o in outcomes if o.startswith("refused")]
    kinds = sorted({re.sub(r" after \d+ ms$", "", o.split(": ", 1)[1]) for o in refused})
    print(f"summary: {len(refused)} of {len(outcomes)} resume attempts refused while connection 1 "
          f"was frozen ({'; '.join(kinds) or 'none'}), "
          f"{sum(o.startswith('setupComplete') for o in outcomes)} accepted\n"
          f"control: after the client's close reached the server, the resume got: {control}")


if __name__ == "__main__":
    asyncio.run(main())
