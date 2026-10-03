"""Gemini Live API: what happens to an in-flight side-effecting tool call when the
connection drops and the client resumes the session?

One run = one Live session carried over two (or more) WebSocket connections:

  1. Connection 1 opens with session resumption on (SessionResumptionConfig(handle=None)).
     Every sessionResumptionUpdate is logged from the raw frame: whether a handle is
     present, `resumable`, `lastConsumedClientMessageIndex`, any other key. The handle
     itself is never written; only its length and a 10-character SHA-256 prefix.
  2. assets/audio/book.wav is streamed as speech: 16 kHz 16-bit mono PCM, 100 ms per
     send_realtime_input(audio=...) call, paced in real time, open-mic silence in
     between, automatic VAD at the server default (as in ../gemini-live-stop-test).
     A book_slot call starts a job on a fake two-phase booking service: prepare takes
     --latency s, then it commits at once. No guard, no dedupe: raw behavior.
  3. At the scenario's disconnect point the harness drops connection 1 abruptly:
     it cancels its own mic and receive tasks (nothing more is sent or read), calls
     transport.abort() on the underlying websockets connection (the TLS/TCP transport
     is torn down at once: no WebSocket close frame, no TLS close_notify), then lets
     the SDK's connect() context exit.
  4. It reconnects at once with the last handle that came with resumable=true and
     the same config, then continues the scenario on the new connection: tool
     response for the pre-disconnect call id (or not), then assets/audio/
     did_you_book.wav ("Did you book it?"). Calls issued after the resume are
     executed and answered like any other call (that is how a double booking shows).
  5. Every scripted action and every server event is written to results/<name>.jsonl
     with ms since session start; one summary row per run goes to results/summary.md.

Scenarios (--scenario):
  R1  disconnect 1.0 s after the toolCall (call pending, before commit). After resume,
      send the response for the old id when the job commits, then ask.
  R2  same disconnect point. Never send the old response; ask 2.0 s after resume.
  R3  disconnect 0.5 s after the commit, before the response is sent. After resume,
      send the old response at once, then ask.
  R4  follow-up question (bring.wav) 0.5 s after the call; disconnect 1.0 s after the
      first model audio chunk that follows the call (model speaking, call pending,
      latency 7.0 s as in stop-test G2). After resume, send the old response when the
      job commits, then ask.

Round 3 (B1/B2/B3, real packet loss; run inside the Linux container built from the
Dockerfile here, with --cap-add NET_ADMIN): at the drop point, blackhole.py adds
iptables rules that silently drop the live connection's 4-tuple both ways (no close,
no RST, the server's segments are never ACKed); a new connection passes. Resume at
detection, then every --bh-period s, up to --bh-max s. B1: R1 timing (model idle).
B2: R4 timing (model speaking). B3: R1 timing, rule removed after 30 s.

Stage 2 (BR1/BR2, client-side recovery; same container and blackhole): at detection,
recovery.py tries to resume for --resume-window s and sends a close on the old socket in
parallel; if no resume is accepted it opens a new session (no handle) and restores the
last turns of the client's transcript plus a status note per side effect from the
service, deduplicates a re-issued book_slot by business key, and never answers the dead
call id. Then "Did you book it?" is asked. BR1: B1 timing. BR2: B2 timing (default
--disconnect-after 0.2, inside the model's audio burst). --no-status-note is the
ablation (summary only). The control BR0 is B1 with --bh-period 1 --bh-max 60.

--save-audio (clip material, as in ../gemini-live-stop-test) writes the model's output
audio of each run to <results-dir>/audio_out/<name>_run<N>_model.wav, plus a JSON
sidecar with each chunk's arrival time and connection and each user clip's send times.
--voice-dir takes the user clips from another folder with the same four file names
(default assets/audio, the macOS `say` voice of every published run; assets/audio/
af_heart is the Kokoro-82M af_heart voice used for the second clip).

A raw tap replaces google.genai.live.ws_connect so every frame is seen, including the
first server message, which the SDK consumes inside connect(). With
--inject-transparent the tap adds "transparent": true to setup.sessionResumption on
the wire (the SDK refuses that field in Gemini Developer API mode; see FINDINGS.md).

The API key is read only from GEMINI_API_KEY (.env here or the environment) and is
never printed. Exit status 3 means a quota or billing error stopped the runs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from google.genai import live as genai_live

from blackhole import Blackhole, FlowWatch, ss_flow
from recovery import Recovery, RecoveryConfig

try:  # websockets is a google-genai dependency
    from websockets.exceptions import ConnectionClosed
except Exception:  # pragma: no cover
    ConnectionClosed = ()  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
LIVE_HOST = "generativelanguage.googleapis.com:443"   # the only CONNECT target allowed
DEFAULT_MODEL = "gemini-3.8-live"
SDK_VERSION = pkg_version("google-genai")

# Same system prompt as the stop-test and commit-guard harnesses.
SYSTEM_INSTRUCTION = (
    "You are a scheduling assistant. When the user asks to book a slot, call "
    "book_slot immediately, then tell the user you are booking it. Keep replies "
    "to one or two short sentences. If the user tells you to stop or cancel, "
    "tell them plainly whether the booking was already made or not."
)
DEFAULT_VOICE_DIR = "assets/audio"   # --voice-dir default: the macOS `say` clips
AUDIO_RATE = 16000
AUDIO_MIME = f"audio/pcm;rate={AUDIO_RATE}"
CHUNK_S = 0.1

SCENARIO_DEFAULTS = {
    #       disconnect_after_s, latency_s, old_response
    "R0": (1.0, 4.0, "never"),   # control: no tool call; drop 1 s after the model's turn
    "R1": (1.0, 4.0, "after_resume"),
    "R2": (1.0, 4.0, "never"),
    "R3": (0.5, 4.0, "after_resume"),
    "R4": (1.0, 7.0, "after_resume"),
    # round 2: network freeze through freeze_proxy.py (R1 drop point, freeze instead)
    "N1": (1.0, 4.0, "after_resume"),   # resume as soon as the client detects the loss
    "N2": (1.0, 4.0, "after_resume"),   # resume 5 s after the freeze
    "N3": (1.0, 4.0, "after_resume"),   # never resume; unfreeze after 30 s, reuse old conn
    # round 3: real packet loss (iptables blackhole of the live flow, in a container)
    "B1": (1.0, 4.0, "after_resume"),   # R1 point: 1.0 s after the toolCall, model idle
    "B2": (1.0, 7.0, "after_resume"),   # R4 point: 1.0 s after model audio, call pending
    "B3": (1.0, 4.0, "after_resume"),   # R1 point; rule removed after 30 s
    # stage 2: client-side recovery (recovery.py) after the same blackhole
    "BR1": (1.0, 4.0, "after_resume"),  # B1 point: 1.0 s after the toolCall, model idle
    "BR2": (0.2, 7.0, "after_resume"),  # B2 run 2 point: 0.2 s into the model's audio
}
OUT_AUDIO_RATE = 24000   # Live API output PCM (16 bit mono), unless the mime type says else
KNOWN_RAW_TOP = {
    "setupComplete", "serverContent", "toolCall", "toolCallCancellation", "goAway",
    "sessionResumptionUpdate", "usageMetadata", "voiceActivity",
    "voiceActivityDetectionSignal",
}

# ---------------------------------------------------------------- redaction --

_SECRETS: list[str] = []
_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_KEY_PARAM_RE = re.compile(r"((?:api[_-]?)?key=)[^&\s'\"]+", re.IGNORECASE)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _KEY_RE.sub("[REDACTED]", text)
    return _KEY_PARAM_RE.sub(r"\1[REDACTED]", text)


def err_text(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:500]


def short_hash(value: str | None) -> str | None:
    """Handles and session ids are logged as a SHA-256 prefix, never in clear."""
    return hashlib.sha256(value.encode()).hexdigest()[:10] if value else None


# ------------------------------------------------- booking (app side, no guard) --

_DAYS = r"today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday"


def canonical_slot(args: dict[str, Any]) -> dict[str, Any]:
    """Business key of a booking: day + 24 h time (copied from
    ../gemini-live-commit-guard/guard_test.py). Used here only to label a re-issued
    call as the same request; nothing is deduplicated."""
    s = re.sub(r"\s+", " ", str(args.get("slot", "")).lower()).strip()
    day = re.search(rf"\b({_DAYS})\b", s)
    tm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b", s)
    if day and tm:
        hour = int(tm.group(1)) % 12 + (12 if tm.group(3) == "p" else 0)
        return {"slot": f"{day.group(1)} {hour:02d}:{int(tm.group(2) or 0):02d}"}
    return {"slot": " ".join(w for w in re.split(r"[\s,]+", s) if w not in ("at", "the", "on"))}


def business_key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:" + json.dumps(canonical_slot(args), sort_keys=True, separators=(",", ":"))


class TwoPhaseBookingService:
    """Fake backend: prepare (latency_s, reversible) then commit (instant,
    irreversible). With no guard the commit follows the prepare at once. Its
    `committed` list is the ground truth."""

    def __init__(self, st: "RunState", latency_s: float) -> None:
        self.st = st
        self.latency_s = latency_s
        self.jobs: dict[str, asyncio.Task] = {}
        self.committed: list[dict[str, Any]] = []
        self._n = 0
        self._ref = 1000

    def start(self, call: dict[str, Any]) -> str:
        self._n += 1
        job_id = f"job-{self._n}"
        self.jobs[job_id] = asyncio.create_task(self._run(job_id, call))
        return job_id

    async def _run(self, job_id: str, call: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self.latency_s)  # prepare
        self.st.emit("service_prepared", job_id=job_id, call_id=call["id"])
        self._ref += 1                        # commit, right away (no guard)
        record = {"status": "booked", "slot": call["args"].get("slot"),
                  "confirmation_id": f"BK-{self._ref}"}
        t = self.st.emit("service_committed", job_id=job_id, call_id=call["id"], **record,
                         connected=self.st.conn is not None and not self.st.conn.gone)
        self.committed.append(record | {"job_id": job_id, "call_id": call["id"], "at_ms": t})
        return record


# ------------------------------------------------------------------ helpers --


class Clock:
    """Milliseconds since session start, from time.monotonic()."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def ms(self) -> int:
        return int(round((time.monotonic() - self.t0) * 1000))


class JsonlWriter:
    def __init__(self, path: Path, scenario: str, verbose: bool) -> None:
        self.scenario = scenario
        self.verbose = verbose
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def write(self, run: int, t_ms: int, event: str, **fields: Any) -> None:
        obj = {"scenario": self.scenario, "run": run, "t_ms": t_ms, "event": event} | fields
        line = redact(json.dumps(obj, ensure_ascii=False, default=str))
        self._fh.write(line + "\n")
        self._fh.flush()
        if self.verbose:
            print("   ", line[:300])

    def close(self) -> None:
        self._fh.close()


def load_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        fmt = (w.getframerate(), w.getsampwidth() * 8, w.getnchannels(), w.getcomptype())
        if fmt != (AUDIO_RATE, 16, 1, "NONE"):
            raise ValueError(f"{path.name}: expected (16000 Hz, 16 bit, 1 ch, NONE), got {fmt}")
        return w.readframes(w.getnframes())


def pcm_seconds(pcm: bytes) -> float:
    return len(pcm) / (AUDIO_RATE * 2)


# ------------------------------------------------------------- raw ws tap --


class Tap:
    """Module-level hook: the connection object the next ws_connect() belongs to."""

    conn: "Conn | None" = None
    inject_transparent: bool = False
    proxy_url: str | None = None   # round 2: route through freeze_proxy (keepalive off)
    direct_no_keepalive: bool = False   # round 3: no proxy at all, keepalive off
    fake_uri: str | None = None   # --fake-live (offline dry run against a local fake server)


TAP = Tap()
_ORIG_WS_CONNECT = genai_live.ws_connect


class TappedWS:
    """Wraps the websockets connection the SDK uses: sees every frame both ways."""

    def __init__(self, ws: Any, conn: "Conn") -> None:
        self.inner = ws
        self.conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def send(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        conn = self.conn
        if conn.frames_sent == 0:
            message = conn.st.on_setup_frame(conn, message)
        else:
            conn.st.on_client_frame(conn, message)
        conn.frames_sent += 1
        return await self.inner.send(message, *args, **kwargs)

    async def recv(self, *args: Any, **kwargs: Any) -> Any:
        try:
            raw = await self.inner.recv(*args, **kwargs)
        except ConnectionClosed as exc:  # type: ignore[misc]
            self.conn.st.on_ws_closed(self.conn, exc)
            raise
        self.conn.st.on_raw(self.conn, raw)
        return raw


class TapConnect:
    """Drop-in for websockets.asyncio.client.connect as used by google.genai.live."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if TAP.proxy_url:
            # Round 2: tunnel through the local freeze proxy. websockets' own keepalive
            # (20 s ping, 20 s timeout by default; google-genai passes no override) is
            # turned off so it cannot close a frozen socket; the harness sends its own
            # pings for loss detection (see liveness()).
            kwargs["proxy"] = TAP.proxy_url
            kwargs["ping_interval"] = None
        elif TAP.direct_no_keepalive:
            # Round 3: straight to the host (no system or environment proxy), and the
            # same keepalive rule as round 2 (harness pings, websockets' own off).
            kwargs["proxy"] = None
            kwargs["ping_interval"] = None
        if TAP.fake_uri:
            # Offline dry run only: a local fake Live server over ws://, no query string
            # (so not even the dummy key leaves the process), no TLS context.
            args = (TAP.fake_uri,) + tuple(args[1:])
            kwargs.pop("ssl", None)
        self._cm = _ORIG_WS_CONNECT(*args, **kwargs)

    async def __aenter__(self) -> Any:
        conn = TAP.conn
        ws = await self._cm.__aenter__()
        if conn is None:
            return ws
        conn.raw_ws = ws
        proxy = conn.st.proxy
        if proxy is not None:
            try:
                port = ws.transport.get_extra_info("sockname")[1]
                tunnel = proxy.tunnel_for_client_port(port)
                conn.tunnel_id = tunnel.id if tunnel else None
            except Exception:
                conn.tunnel_id = None
        extra = {}
        if TAP.direct_no_keepalive:
            try:
                extra = {"local_port": ws.transport.get_extra_info("sockname")[1],
                         "peer": ws.transport.get_extra_info("peername")[0]}
            except Exception:
                pass
        conn.ws_open_ms = conn.st.emit("ws_open", conn=conn.n, tunnel=conn.tunnel_id, **extra)
        return TappedWS(ws, conn)

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._cm.__aexit__(*exc)


genai_live.ws_connect = TapConnect  # type: ignore[assignment]


# -------------------------------------------------------------------- audio --


@dataclass
class Utterance:
    label: str
    chunks: list[bytes]
    started: asyncio.Future
    ended: asyncio.Future


class Mic:
    """Simulated open microphone bound to one connection (as in the stop test):
    100 ms chunks paced in real time, silence while no utterance is queued."""

    def __init__(self, st: "RunState", conn: "Conn") -> None:
        self.st = st
        self.conn = conn
        self.chunk_bytes = int(AUDIO_RATE * CHUNK_S) * 2
        self.silence = bytes(self.chunk_bytes)
        self.queue: deque[Utterance] = deque()
        self.wake = asyncio.Event()
        self.current: Utterance | None = None

    def say(self, label: str, pcm: bytes) -> Utterance:
        loop = asyncio.get_running_loop()
        chunks = [pcm[i:i + self.chunk_bytes] for i in range(0, len(pcm), self.chunk_bytes)]
        utt = Utterance(label, chunks, loop.create_future(), loop.create_future())
        self.queue.append(utt)
        self.wake.set()
        return utt

    def fail_pending(self) -> None:
        for utt in ([self.current] if self.current else []) + list(self.queue):
            for fut in (utt.started, utt.ended):
                if not fut.done():
                    fut.set_result(None)

    def stopped(self) -> bool:
        return self.conn.gone or self.conn.dropping or self.conn.suspended or self.st.ending

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        st, conn = self.st, self.conn
        idx = 0
        next_t = loop.time()
        try:
            while not self.stopped():
                self.wake.clear()
                if self.current is None and self.queue:
                    self.current, idx = self.queue.popleft(), 0
                cur = self.current
                data = cur.chunks[idx] if cur else self.silence
                try:
                    await conn.session.send_realtime_input(
                        audio=types.Blob(data=data, mime_type=AUDIO_MIME))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self.stopped():
                        st.error("send_audio", err_text(exc), conn=conn.n)
                    return
                if cur is not None:
                    if idx == 0:
                        t = st.emit("user_audio_start", conn=conn.n, label=cur.label,
                                    audio_s=round(sum(map(len, cur.chunks)) / (AUDIO_RATE * 2), 3),
                                    chunks=len(cur.chunks), client_msg_index=conn.client_index)
                        cur.started.set_result(t)
                        st.clip_sends.append({"label": cur.label, "conn": conn.n,
                                              "sent_start_ms": t, "sent_end_ms": None})
                    idx += 1
                    if idx == len(cur.chunks):
                        t = st.emit("user_audio_end", conn=conn.n, label=cur.label,
                                    client_msg_index=conn.client_index)
                        st.user_audio_ends.append(t)
                        if st.clip_sends and st.clip_sends[-1]["label"] == cur.label:
                            st.clip_sends[-1]["sent_end_ms"] = t
                        cur.ended.set_result(t)
                        self.current = None
                next_t += len(data) / (AUDIO_RATE * 2)
                now = loop.time()
                if next_t < now - 0.5:
                    next_t = now
                delay = next_t - now
                if self.current is None and not self.queue:
                    try:
                        await asyncio.wait_for(self.wake.wait(), max(0.0, delay))
                        next_t = loop.time()
                    except TimeoutError:
                        pass
                elif delay > 0:
                    await asyncio.sleep(delay)
        finally:
            self.fail_pending()


# -------------------------------------------------------------------- state --


@dataclass
class Conn:
    """One WebSocket connection of the session."""

    n: int
    st: "RunState"
    resume_handle_sha: str | None
    cm: Any = None
    session: Any = None
    raw_ws: Any = None
    mic: Mic | None = None
    mic_task: asyncio.Task | None = None
    recv_task: asyncio.Task | None = None
    frames_sent: int = 0      # incl. the setup frame
    client_index: int = 0     # client messages after setup, first one = 1
    frames_recv: int = 0
    connect_start_ms: int | None = None
    ws_open_ms: int | None = None
    first_raw_ms: int | None = None
    first_raw_keys: list[str] = field(default_factory=list)
    first_raw_has_setup_complete: bool | None = None
    session_id_sha: str | None = None
    connected_ms: int | None = None       # connect() returned (setup done)
    first_after_setup_ms: int | None = None
    first_after_setup_keys: list[str] = field(default_factory=list)
    gone: bool = False         # closed by the server or the network
    dropping: bool = False     # we are aborting it on purpose
    close_info: dict | None = None
    sent_types: dict[str, int] = field(default_factory=dict)
    # round 2
    tunnel_id: int | None = None
    suspended: bool = False    # client gave up on it (loss detected) but keeps it open
    last_rx_ms: int = 0        # last server frame or pong
    pongs_ms: list = field(default_factory=list)   # (sent_ms, rtt_ms)
    pings_sent: int = 0
    lost_ms: int | None = None
    liveness_task: asyncio.Task | None = None
    log_frames_from_ms: int | None = None   # N3: log every frame after the unfreeze
    exited: bool = False       # SDK connect() context already exited


@dataclass
class RunState:
    args: argparse.Namespace
    run: int
    clock: Clock
    out: JsonlWriter
    client: Any
    wall: str = ""
    conns: list[Conn] = field(default_factory=list)
    conn: Conn | None = None
    service: TwoPhaseBookingService = field(init=False)
    ending: bool = False
    calls: list[dict] = field(default_factory=list)
    tool_call_at_ms: int | None = None
    first_call: dict | None = None
    first_commit_ms: int | None = None
    updates: list[dict] = field(default_factory=list)
    usable_handle: dict | None = None     # {"value", "at_ms", "conn", "sha"}
    go_aways: list[dict] = field(default_factory=list)
    disconnect: dict | None = None
    reconnects: list[dict] = field(default_factory=list)
    resume_failed: str | None = None
    resume_attempts: list[dict] = field(default_factory=list)   # failed attempts only
    no_tool_call: bool = False
    tool_responses: list[dict] = field(default_factory=list)
    old_response: dict | None = None
    texts: list[tuple[int, int, int, str]] = field(default_factory=list)  # ms, conn, turn, text
    input_texts: list[tuple[int, int, str]] = field(default_factory=list)
    turn_idx: int = 0
    last_output_ms: int = -1
    gen_complete_ms: list[int] = field(default_factory=list)
    turn_complete_ms: list[int] = field(default_factory=list)
    interrupted_ms: list[int] = field(default_factory=list)
    cancellations: list[dict] = field(default_factory=list)
    audio_seg: dict | None = None
    speech_after_call_ms: int | None = None
    ask: dict | None = None
    followup: dict | None = None
    errors: list[str] = field(default_factory=list)
    server_errors: list[dict] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)
    tool_call_event: asyncio.Event = field(default_factory=asyncio.Event)
    commit_event: asyncio.Event = field(default_factory=asyncio.Event)
    resumed_event: asyncio.Event = field(default_factory=asyncio.Event)
    old_response_event: asyncio.Event = field(default_factory=asyncio.Event)
    speech_event: asyncio.Event = field(default_factory=asyncio.Event)
    done_reason: str = "unknown"
    # round 2
    proxy: Any = None
    link_lost_event: asyncio.Event = field(default_factory=asyncio.Event)
    freeze: dict | None = None
    asks: list[dict] = field(default_factory=list)
    proxy_events: list[dict] = field(default_factory=list)
    old_frames: list[dict] = field(default_factory=list)
    old_close: dict | None = None
    resume_phase: str = "first"
    recovery: dict | None = None
    # round 3
    bh: Any = None                      # blackhole.Blackhole (shared across runs)
    watch: Any = None                   # blackhole.FlowWatch on the old flow
    flow: dict | None = None            # {"local": [ip, port], "remote": [ip, port]}
    bh_info: dict | None = None         # add() result + at_ms (+ unblock_ms for B3)
    bh_pkts: list[dict] = field(default_factory=list)       # old-flow packets after add()
    bh_pkts_before: dict = field(default_factory=lambda: {"c2s": 0, "s2c": 0})
    bh_counters: list[dict] = field(default_factory=list)
    bh_ss: list[dict] = field(default_factory=list)
    # stage 2 (recovery.py)
    rec: Any = None                     # recovery.Recovery (BR scenarios)
    user_audio_ends: list[int] = field(default_factory=list)
    play_end_ms: float = -1.0           # end of playback of the model audio received so far
    first_audio_by_conn: dict = field(default_factory=dict)
    last_audio_chunk_ms: int | None = None
    loss_audio: dict | None = None      # what had been heard when the flow was blackholed
    # --save-audio (clip material only; changes nothing in the run)
    audio_out: list[dict] = field(default_factory=list)    # per model chunk: t_ms, conn, rate
    audio_data: list[bytes] = field(default_factory=list)  # model chunk bytes
    clip_sends: list[dict] = field(default_factory=list)   # user clips: label, conn, send times

    def __post_init__(self) -> None:
        self.service = TwoPhaseBookingService(self, self.args.latency)

    def emit(self, event: str, **fields: Any) -> int:
        t = self.clock.ms()
        self.out.write(self.run, t, event, **fields)
        return t

    def error(self, where: str, detail: str, **fields: Any) -> None:
        self.errors.append(f"{where}: {detail}")
        self.emit("error", where=where, detail=detail, **fields)

    # ---- raw tap callbacks
    def on_setup_frame(self, conn: Conn, message: Any) -> Any:
        try:
            data = json.loads(message)
        except Exception:
            self.emit("setup_frame_unparseable", conn=conn.n)
            return message
        setup = data.get("setup", {}) if isinstance(data, dict) else {}
        if TAP.inject_transparent:
            setup.setdefault("sessionResumption", {})["transparent"] = True
            message = json.dumps(data)
        sr = setup.get("sessionResumption")
        self.emit("setup_sent", conn=conn.n, setup_keys=sorted(setup),
                  session_resumption=None if sr is None else {
                      "keys": sorted(sr), "handle_present": bool(sr.get("handle")),
                      "handle_sha": short_hash(sr.get("handle")),
                      "transparent": sr.get("transparent")},
                  transparent_injected_on_wire=TAP.inject_transparent)
        return message

    def on_client_frame(self, conn: Conn, message: Any) -> None:
        conn.client_index += 1
        head = message[:40] if isinstance(message, str) else ""
        m = re.match(r'\{"(\w+)"', head)
        kind = m.group(1) if m else "?"
        conn.sent_types[kind] = conn.sent_types.get(kind, 0) + 1
        if kind != "realtime_input":
            self.emit("client_frame", conn=conn.n, kind=kind, client_msg_index=conn.client_index,
                      body=redact(message)[:600])

    def on_raw(self, conn: Conn, raw: Any) -> None:
        t = self.clock.ms()
        conn.frames_recv += 1
        conn.last_rx_ms = t
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            self.emit("raw_unparseable", conn=conn.n, size=len(raw) if raw else 0)
            return
        if not isinstance(data, dict):
            return
        keys = sorted(data)
        if conn.log_frames_from_ms is not None:
            rec = {"conn": conn.n, "since_unfreeze_ms": t - conn.log_frames_from_ms,
                   **summarize_frame(data)}
            self.old_frames.append(rec | {"at_ms": t})
            self.emit("frame_after_unfreeze", **rec)
        if conn.frames_recv == 1:
            conn.first_raw_ms = t
            conn.first_raw_keys = keys
            conn.first_raw_has_setup_complete = "setupComplete" in data
            sc = data.get("setupComplete")
            if isinstance(sc, dict):
                conn.session_id_sha = short_hash(sc.get("sessionId"))
            self.emit("first_server_message", conn=conn.n, keys=keys,
                      setup_complete=conn.first_raw_has_setup_complete,
                      setup_complete_fields=sorted(sc) if isinstance(sc, dict) else None,
                      session_id_sha=conn.session_id_sha,
                      since_connect_start_ms=t - conn.connect_start_ms
                      if conn.connect_start_ms is not None else None,
                      since_abort_ms=(t - self.disconnect["abort_ms"])
                      if self.disconnect and conn.n > 1 else None)
        elif conn.first_after_setup_ms is None:
            conn.first_after_setup_ms = t
            conn.first_after_setup_keys = keys
            self.emit("first_message_after_setup", conn=conn.n, keys=keys,
                      since_abort_ms=(t - self.disconnect["abort_ms"])
                      if self.disconnect and conn.n > 1 else None)
            if "setupComplete" in data:
                self.emit("setup_complete_again", conn=conn.n)
        elif "setupComplete" in data:
            self.emit("setup_complete_again", conn=conn.n)
        if "error" in data:
            e = data["error"]
            self.server_errors.append({"at_ms": t, "conn": conn.n, "error": e})
            self.error("server_error_message", redact(json.dumps(e))[:400], conn=conn.n)
        sru = data.get("sessionResumptionUpdate")
        if isinstance(sru, dict):
            handle = sru.get("newHandle") or None
            rec = {"at_ms": t, "conn": conn.n, "keys": sorted(sru),
                   "resumable": sru.get("resumable"), "handle_present": bool(handle),
                   "handle_len": len(handle) if handle else 0, "handle_sha": short_hash(handle),
                   "last_consumed_client_message_index": sru.get("lastConsumedClientMessageIndex"),
                   "client_msg_index_now": conn.client_index,
                   "pending_call_ids": self.pending_call_ids(),
                   "since_tool_call_ms": (t - self.tool_call_at_ms)
                   if self.tool_call_at_ms is not None else None}
            self.updates.append(rec)
            self.emit("session_resumption_update", **rec)
            if handle and sru.get("resumable"):
                self.usable_handle = {"value": handle, "at_ms": t, "conn": conn.n,
                                      "sha": short_hash(handle)}
        if "goAway" in data:
            rec = {"at_ms": t, "conn": conn.n, "payload": data["goAway"]}
            self.go_aways.append(rec)
            self.emit("go_away", **rec)
        for k in ("toolCall", "toolCallCancellation"):
            if k in data:
                self.emit(f"raw_{k}", conn=conn.n, payload=data[k])
        unknown = sorted(set(data) - KNOWN_RAW_TOP - {"error"})
        if unknown:
            self.emit("raw_unknown_keys", conn=conn.n, keys=unknown,
                      payload=redact(json.dumps({k: data[k] for k in unknown}))[:600])

    def on_ws_closed(self, conn: Conn, exc: BaseException) -> None:
        if conn.gone:
            return
        rcvd = getattr(exc, "rcvd", None)
        info = {"code": getattr(rcvd, "code", None),
                "reason": redact(str(getattr(rcvd, "reason", "") or ""))[:400]}
        self.mark_gone(conn, info, "ws_recv")

    def mark_gone(self, conn: Conn, info: dict, where: str) -> None:
        if conn.gone:
            return
        conn.gone = True
        conn.close_info = info | {"at_ms": self.clock.ms()}
        expected = conn.dropping or self.ending
        self.emit("ws_closed", conn=conn.n, where=where, expected=expected, **info)
        if not expected:
            self.errors.append(f"ws_closed conn={conn.n} code={info.get('code')} "
                               f"{info.get('reason', '')}".strip())

    # ---- bookkeeping
    def pending_call_ids(self) -> list[str]:
        responded = {r["call_id"] for r in self.tool_responses}
        return [c["id"] for c in self.calls if c["id"] not in responded]

    def audio_chunk(self, conn: Conn, nbytes: int, rate: int = OUT_AUDIO_RATE,
                    data: bytes | None = None) -> None:
        t = self.clock.ms()
        if data is not None and getattr(self.args, "save_audio", False):
            self.audio_out.append({"t_ms": t, "conn": conn.n, "turn": self.turn_idx,
                                   "rate": rate})
            self.audio_data.append(data)
        # Playback model for the user-visible gap: chunks play back to back in arrival
        # order, starting on arrival when nothing is playing; `interrupted` flushes.
        self.play_end_ms = max(self.play_end_ms, t) + nbytes / (rate * 2) * 1000
        self.first_audio_by_conn.setdefault(conn.n, t)
        self.last_audio_chunk_ms = t
        if self.audio_seg is None:
            self.audio_seg = {"conn": conn.n, "first_ms": t, "last_ms": t, "chunks": 0,
                              "bytes": 0}
            self.emit("audio_start", conn=conn.n)
        self.audio_seg["last_ms"] = t
        self.audio_seg["chunks"] += 1
        self.audio_seg["bytes"] += nbytes
        self.last_output_ms = t
        if (self.tool_call_at_ms is not None and self.speech_after_call_ms is None
                and conn.n == 1):
            self.speech_after_call_ms = t
            self.emit("model_speech_after_tool_call", conn=conn.n,
                      since_tool_call_ms=t - self.tool_call_at_ms,
                      pending_call_ids=self.pending_call_ids())
            self.speech_event.set()

    def flush_audio(self, reason: str) -> None:
        if self.audio_seg is not None:
            self.emit("audio_segment", closed_by=reason, **self.audio_seg)
            self.audio_seg = None

    def model_idle(self) -> bool:
        return self.last_output_ms <= max(self.gen_complete_ms + self.turn_complete_ms,
                                          default=-1)


# ------------------------------------------------------------------- config --


def build_config(args: argparse.Namespace, handle: str | None) -> types.LiveConnectConfig:
    decl = types.FunctionDeclaration(
        name="book_slot",
        description="Book an appointment slot for the user. Returns a confirmation.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"slot": types.Schema(
                type=types.Type.STRING, description="The slot to book, e.g. 'tomorrow 3pm'.")},
            required=["slot"]),
    )
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM_INSTRUCTION)]),
        tools=[types.Tool(function_declarations=[decl])],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        # transparent is not set: google-genai 2.25.0 raises ValueError for it in
        # Gemini Developer API mode (see --inject-transparent).
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )


# ------------------------------------------------------------ connections --


async def open_connection(st: RunState, handle: str | None) -> Conn:
    conn = Conn(n=len(st.conns) + 1, st=st, resume_handle_sha=short_hash(handle))
    st.conns.append(conn)
    TAP.conn = conn
    conn.connect_start_ms = st.emit("connect_start", conn=conn.n, resume=handle is not None,
                                    handle_sha=conn.resume_handle_sha)
    conn.cm = st.client.aio.live.connect(model=st.args.model,
                                         config=build_config(st.args, handle))
    try:
        conn.session = await conn.cm.__aenter__()
    except BaseException:
        conn.gone = True
        raise
    conn.connected_ms = st.emit(
        "connected", conn=conn.n,
        sdk_setup_complete=getattr(conn.session, "setup_complete", None) is not None,
        since_connect_start_ms=st.clock.ms() - conn.connect_start_ms,
        since_abort_ms=(st.clock.ms() - st.disconnect["abort_ms"]) if st.disconnect else None)
    st.conn = conn
    if st.rec is not None and conn.n == 1:
        st.rec.attach(conn.session)
    conn.recv_task = asyncio.create_task(receiver(st, conn))
    conn.mic = Mic(st, conn)
    conn.mic_task = asyncio.create_task(conn.mic.run())
    st.tasks += [conn.recv_task, conn.mic_task]
    if st.proxy is not None or st.args.scenario.startswith("B"):
        conn.last_rx_ms = st.clock.ms()
        conn.liveness_task = asyncio.create_task(liveness(st, conn, act=True))
        st.tasks.append(conn.liveness_task)
    return conn


async def stop_conn_tasks(conn: Conn) -> None:
    tasks = [t for t in (conn.mic_task, conn.recv_task, conn.liveness_task) if t is not None]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def drop_connection(st: RunState, why: str) -> None:
    """Abrupt disconnect: stop our own tasks, abort the transport (no close frame,
    no TLS close_notify), then let the SDK context exit."""
    conn = st.conn
    assert conn is not None
    last = st.updates[-1] if st.updates else None
    conn.dropping = True
    t0 = st.emit("disconnect_start", conn=conn.n, why=why,
                 since_tool_call_ms=st.clock.ms() - st.tool_call_at_ms
                 if st.tool_call_at_ms is not None else None,
                 since_commit_ms=st.clock.ms() - st.first_commit_ms
                 if st.first_commit_ms is not None else None,
                 pending_call_ids=st.pending_call_ids(),
                 model_idle=st.model_idle(),
                 last_update=None if last is None else {
                     k: last[k] for k in ("at_ms", "resumable", "handle_present", "handle_sha",
                                          "last_consumed_client_message_index")},
                 usable_handle=None if st.usable_handle is None else {
                     "at_ms": st.usable_handle["at_ms"], "sha": st.usable_handle["sha"]},
                 client_msg_index=conn.client_index)
    await stop_conn_tasks(conn)
    st.flush_audio("disconnect")
    ws = conn.raw_ws
    state_before = str(getattr(ws, "state", None))
    if st.args.drop_mode == "abort":
        method = "websockets ClientConnection.transport.abort()"
        try:
            ws.transport.abort()
        except Exception as exc:
            st.error("transport_abort", err_text(exc), conn=conn.n)
    else:
        method = "clean close: SDK context exit -> websockets close(1000) handshake"
    abort_ms = st.emit("transport_aborted", conn=conn.n, method=method,
                       ws_state_before=state_before, tasks_stopped_ms=st.clock.ms() - t0)
    await asyncio.sleep(0)
    exit_err = None
    try:
        await asyncio.wait_for(conn.cm.__aexit__(None, None, None), 5)
    except Exception as exc:
        exit_err = err_text(exc)
    conn.exited = True
    conn.gone = True
    conn.close_info = {"code": getattr(ws, "close_code", None),
                       "reason": str(getattr(ws, "close_reason", "") or "")}
    st.emit("connection_dropped", conn=conn.n, ws_state_after=str(getattr(ws, "state", None)),
            local_close_code=conn.close_info["code"], sdk_context_exit_error=exit_err,
            since_abort_ms=st.clock.ms() - abort_ms)
    st.disconnect = {"start_ms": t0, "abort_ms": abort_ms, "conn": conn.n, "why": why,
                     "last_update": last, "pending": st.pending_call_ids(),
                     "model_idle": st.model_idle(),
                     "usable_handle_ms": st.usable_handle["at_ms"] if st.usable_handle else None}


async def reconnect(st: RunState, reason: str) -> bool:
    h = st.usable_handle
    st.emit("reconnect_start", reason=reason, handle_sha=h["sha"] if h else None,
            handle_from_ms=h["at_ms"] if h else None,
            handle_from_conn=h["conn"] if h else None,
            handle_before_tool_call=(h["at_ms"] < st.tool_call_at_ms)
            if h and st.tool_call_at_ms is not None else None)
    if h is None:
        st.resume_failed = "no resumable handle received before the disconnect"
        st.error("reconnect", st.resume_failed)
        return False
    # First attempt at once, then --reconnect-retries more after 1, 2, 4, ... s (x base).
    if st.args.retry_fixed and st.resume_phase == "while_frozen":
        delays = [st.args.resume_delay] + [st.args.retry_fixed] * st.args.reconnect_retries
    else:
        delays = ([st.args.resume_delay]
                  + [st.args.retry_base * 2 ** i for i in range(st.args.reconnect_retries)])
    t = st.clock.ms()
    conn = None
    for attempt, delay in enumerate(delays, 1):
        if delay:
            await asyncio.sleep(delay)
        t_try = st.clock.ms()
        try:
            conn = await asyncio.wait_for(open_connection(st, h["value"]),
                                          st.args.attempt_timeout)
            break
        except Exception as exc:
            if isinstance(exc, TimeoutError) and st.conns:
                st.conns[-1].gone = True
                exc = TimeoutError(f"no setupComplete within {st.args.attempt_timeout}s")
            failed = st.conns[-1] if st.conns else None
            rec_fail = {"attempt": attempt, "phase": st.resume_phase, "start_ms": t_try,
                        "error": err_text(exc),
                        "ms_to_error": st.clock.ms() - t_try,
                        "since_abort_ms": (st.clock.ms() - st.disconnect["abort_ms"])
                        if st.disconnect else None,
                        "setup_sent": bool(failed and failed.frames_sent),
                        "server_frames": failed.frames_recv if failed else None,
                        "close": failed.close_info if failed else None}
            st.resume_attempts.append(rec_fail)
            st.emit("reconnect_attempt_failed", **rec_fail)
    if conn is None:
        st.resume_failed = (f"{len(delays)} attempts failed; last: "
                            f"{st.resume_attempts[-1]['error'] if st.resume_attempts else '?'}")
        st.error("reconnect", st.resume_failed)
        return False
    rec = {"reason": reason, "phase": st.resume_phase, "conn": conn.n, "start_ms": t,
           "attempts": attempt,
           "first_raw_ms": conn.first_raw_ms, "connected_ms": conn.connected_ms,
           "first_raw_has_setup_complete": conn.first_raw_has_setup_complete,
           "first_raw_keys": conn.first_raw_keys, "handle_at_ms": h["at_ms"],
           "handle_conn": h["conn"],
           "abort_to_first_raw_ms": (conn.first_raw_ms - st.disconnect["abort_ms"])
           if conn.first_raw_ms is not None and st.disconnect else None,
           "abort_to_connected_ms": (conn.connected_ms - st.disconnect["abort_ms"])
           if st.disconnect else None}
    st.reconnects.append(rec)
    st.emit("resumed", **rec)
    st.resumed_event.set()
    return True


# ------------------------------------------------------------- receive side --


def handle_tool_call(st: RunState, conn: Conn, msg: types.LiveServerMessage) -> None:
    for fc in msg.tool_call.function_calls or []:
        args = dict(fc.args or {})
        key = business_key(fc.name or "", args)
        prior = [c for c in st.calls if c["name"] == fc.name]
        same_id = [c["id"] for c in prior if c["id"] == fc.id]
        same_key = [c["id"] for c in prior if c["key"] == key]
        t = st.clock.ms()
        call = {"id": fc.id, "name": fc.name, "args": args, "key": key, "conn": conn.n,
                "at_ms": t, "after_resume": conn.n > 1,
                "old": conn.n == 1}  # issued before the disconnect: held until resume
        st.calls.append(call)
        st.emit("tool_call_received", conn=conn.n, call_id=fc.id, name=fc.name, args=args,
                key=key, repeats_id=same_id, repeats_key_of=same_key,
                after_resume=conn.n > 1,
                since_resume_ms=(t - st.reconnects[-1]["connected_ms"])
                if conn.n > 1 and st.reconnects else None)
        if fc.name != "book_slot":
            fr = types.FunctionResponse(id=fc.id, name=fc.name,
                                        response={"error": f"unknown function {fc.name}"})
            st.tasks.append(asyncio.create_task(
                conn.session.send_tool_response(function_responses=[fr])))
            continue
        if st.rec is not None:   # stage 2: same business key as a job in the ledger?
            dup = st.rec.on_call(fc.id, fc.name, args)
            if dup is not None:
                call["deduped_to"] = dup.call_id
                st.emit("tool_call_deduplicated", conn=conn.n, call_id=fc.id,
                        original_call_id=dup.call_id, original_state=dup.state)
                continue
        if st.first_call is None:
            st.first_call = call
            st.tool_call_at_ms = t
            st.tool_call_event.set()
        call["job_id"] = st.service.start(call)
        if st.rec is not None:
            st.rec.call_started(fc.id, fc.name, args)
        st.emit("service_job_started", conn=conn.n, job_id=call["job_id"], call_id=fc.id,
                latency_s=st.args.latency)
        st.tasks.append(asyncio.create_task(respond_when_done(st, call)))


async def respond_when_done(st: RunState, call: dict) -> None:
    task = st.service.jobs[call["job_id"]]
    await asyncio.wait([task])
    if task.cancelled():
        st.emit("service_job_cancelled", job_id=call["job_id"], call_id=call["id"])
        return
    record = task.result()
    if st.rec is not None:
        st.rec.call_finished(call["id"], "committed", dict(record))
    if call is st.first_call:
        st.first_commit_ms = st.service.committed[0]["at_ms"] if st.service.committed else None
        st.commit_event.set()
    if call["old"]:
        if st.args.old_response == "never":
            st.emit("old_response_withheld", call_id=call["id"])
            return
        if st.rec is not None:
            # Stage 2: the id is answered only if the same session came back (resume);
            # after a fallback it died with the old session and the ledger reports it.
            await st.rec.recovered.wait()
            if st.rec.mode != "resumed":
                st.emit("old_response_not_sent", call_id=call["id"], mode=st.rec.mode,
                        reason="the call id belongs to the lost session")
                st.old_response = {"call_id": call["id"], "sent_ms": None,
                                   "error": f"not sent: recovery mode {st.rec.mode}"}
                st.old_response_event.set()
                return
        if not st.resumed_event.is_set():
            st.emit("old_response_held_until_resume", call_id=call["id"])
            await st.resumed_event.wait()
    conn = st.conn
    if conn is None or conn.gone:
        st.error("send_tool_response", "no open connection", call_id=call["id"])
        if call["old"]:
            st.old_response = {"call_id": call["id"], "sent_ms": None, "error": "no connection"}
            st.old_response_event.set()
        return
    fr = types.FunctionResponse(id=call["id"], name=call["name"], response=dict(record))
    try:
        await conn.session.send_tool_response(function_responses=[fr])
        t = st.emit("tool_response_sent", conn=conn.n, call_id=call["id"], old=call["old"],
                    call_conn=call["conn"], response=fr.response,
                    client_msg_index=conn.client_index)
        st.tool_responses.append({"at_ms": t, "call_id": call["id"], "conn": conn.n,
                                  "old": call["old"], "confirmation_id":
                                  record["confirmation_id"]})
        if st.rec is not None:
            st.rec.call_responded(call["id"])
        if call["old"] and st.old_response is None:
            st.old_response = {"call_id": call["id"], "sent_ms": t, "conn": conn.n,
                               "confirmation_id": record["confirmation_id"], "error": None}
    except Exception as exc:
        st.error("send_tool_response", err_text(exc), call_id=call["id"])
        if call["old"] and st.old_response is None:
            st.old_response = {"call_id": call["id"], "sent_ms": st.clock.ms(),
                               "conn": conn.n, "error": err_text(exc)}
    if call["old"]:
        st.old_response_event.set()


def handle_server_content(st: RunState, conn: Conn, sc: types.LiveServerContent) -> None:
    if sc.model_turn and sc.model_turn.parts:
        for part in sc.model_turn.parts:
            if part.inline_data is not None and (part.inline_data.mime_type or "").startswith("audio"):
                m = re.search(r"rate=(\d+)", part.inline_data.mime_type or "")
                st.audio_chunk(conn, len(part.inline_data.data or b""),
                               int(m.group(1)) if m else OUT_AUDIO_RATE,
                               data=part.inline_data.data or b"")
            elif part.text:
                if part.thought:
                    st.emit("model_thought", conn=conn.n, text=part.text[:400])
                else:
                    t = st.emit("model_text", conn=conn.n, text=part.text)
                    st.texts.append((t, conn.n, st.turn_idx, part.text))
                    st.last_output_ms = t
    if sc.output_transcription and sc.output_transcription.text:
        t = st.emit("model_transcript", conn=conn.n, text=sc.output_transcription.text)
        st.texts.append((t, conn.n, st.turn_idx, sc.output_transcription.text))
        st.last_output_ms = t
        if st.rec is not None:
            st.rec.transcript.add("model", sc.output_transcription.text, t)
    if sc.input_transcription and sc.input_transcription.text:
        t = st.emit("input_transcript", conn=conn.n, text=sc.input_transcription.text)
        st.input_texts.append((t, conn.n, sc.input_transcription.text))
        if st.rec is not None:
            st.rec.transcript.add("user", sc.input_transcription.text, t)
    extra = {k: getattr(sc, k) for k in ("turn_complete_reason", "waiting_for_input",
                                         "interaction_status")
             if getattr(sc, k, None) is not None}
    if sc.interrupted:
        st.flush_audio("interrupted")
        t = st.emit("interrupted", conn=conn.n, **extra)
        st.interrupted_ms.append(t)
        st.play_end_ms = min(st.play_end_ms, t)   # a client drops queued audio here
        st.turn_idx += 1
        if st.rec is not None:
            st.rec.transcript.close_turn()
    if sc.generation_complete:
        st.flush_audio("generation_complete")
        st.gen_complete_ms.append(st.emit("generation_complete", conn=conn.n, **extra))
    if sc.turn_complete:
        st.flush_audio("turn_complete")
        st.turn_complete_ms.append(st.emit("turn_complete", conn=conn.n, **extra))
        st.turn_idx += 1
        if st.rec is not None:
            st.rec.transcript.close_turn()


async def receiver(st: RunState, conn: Conn) -> None:
    consecutive = 0
    while not conn.gone:
        try:
            async for msg in conn.session.receive():
                consecutive = 0
                if msg.server_content:
                    handle_server_content(st, conn, msg.server_content)
                if msg.tool_call:
                    handle_tool_call(st, conn, msg)
                if msg.tool_call_cancellation:
                    ids = list(msg.tool_call_cancellation.ids or [])
                    t = st.emit("tool_call_cancellation", conn=conn.n, ids=ids)
                    st.cancellations.append({"at_ms": t, "conn": conn.n, "ids": ids})
                if msg.voice_activity:
                    va = msg.voice_activity
                    st.emit("voice_activity", conn=conn.n,
                            type=str(getattr(va.voice_activity_type, "value",
                                             va.voice_activity_type)),
                            audio_offset=va.audio_offset)
                if msg.usage_metadata:
                    um = msg.usage_metadata
                    st.emit("usage_metadata", conn=conn.n,
                            total_token_count=um.total_token_count)
                if msg.setup_complete:
                    st.emit("sdk_setup_complete_in_stream", conn=conn.n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if isinstance(exc, errors.APIError) and isinstance(code, int) and 1000 <= code < 5000:
                st.mark_gone(conn, {"code": code, "reason": err_text(exc)[:400]}, "sdk_receive")
                return
            st.error("receive", err_text(exc), conn=conn.n)
            consecutive += 1
            if conn.gone or consecutive >= 5:
                return
            await asyncio.sleep(0.05)


# ------------------------------------------------------------------ script --


async def sleep_until(st: RunState, target_ms: float) -> None:
    await asyncio.sleep(max(0.0, (target_ms - st.clock.ms()) / 1000))


async def ensure_connected(st: RunState) -> bool:
    if st.conn is not None and not st.conn.gone:
        return True
    if len(st.reconnects) >= st.args.max_reconnects:
        st.error("reconnect", f"connection {st.conn.n if st.conn else '?'} is closed and "
                 f"--max-reconnects {st.args.max_reconnects} is reached")
        return False
    st.emit("recovery_reconnect", closed_conn=st.conn.n if st.conn else None,
            close_info=st.conn.close_info if st.conn else None)
    return await reconnect(st, "server_closed_after_resume")


async def wait_ask_settled(st: RunState, t_ref: int) -> str:
    """Ask once the model has reacted to the old response (or stayed silent): at least
    --ask-min-after-response s after it, model not mid-turn, 1 s without output."""
    a = st.args
    while True:
        now = st.clock.ms()
        if now - t_ref >= a.ask_max_wait * 1000:
            return "ask_max_wait"
        if st.conn is None or st.conn.gone:
            return "connection_closed"
        if (now - t_ref >= a.ask_min_after_response * 1000 and st.model_idle()
                and now - st.last_output_ms >= 1000):
            return "settled"
        await asyncio.sleep(0.05)


async def script(st: RunState) -> None:
    a = st.args
    conn = st.conn
    assert conn is not None and conn.mic is not None
    if a.scenario == "R0":
        await control_until_drop_point(st)
        await drop_connection(st, f"R0 control: {a.disconnect_after}s after the model's "
                                  "turn and the next resumption update")
        if await reconnect(st, "scripted_disconnect"):
            await after_resume(st)
        return
    book = conn.mic.say("book_request", a.clips["book"])
    if await book.ended is None:
        st.no_tool_call = True
        return
    try:
        await asyncio.wait_for(st.tool_call_event.wait(), a.tool_call_wait)
    except TimeoutError:
        st.no_tool_call = True
        st.emit("no_tool_call", waited_s=a.tool_call_wait)
        return
    tc = st.tool_call_at_ms
    if a.scenario.startswith("N"):
        await freeze_flow(st, tc)
        return
    if a.scenario.startswith("BR"):
        await recovery_flow(st, tc)
        return
    if a.scenario.startswith("B"):
        await blackhole_flow(st, tc)
        return
    # ---- disconnect point
    why = ""
    if a.scenario in ("R1", "R2"):
        target = tc + a.disconnect_after * 1000
        why = f"{a.disconnect_after}s after toolCall"
    elif a.scenario == "R3":
        try:
            await asyncio.wait_for(st.commit_event.wait(), a.latency + 10)
        except TimeoutError:
            st.error("script", "no commit for the first call")
            return
        target = st.first_commit_ms + a.disconnect_after * 1000
        why = f"{a.disconnect_after}s after commit, tool response not sent"
    else:  # R4
        await sleep_until(st, tc + a.followup_after_tool_call * 1000)
        conn.mic.say("followup", a.clips["followup"])
        st.followup = {"queued_ms": st.emit("followup_queued",
                                            since_tool_call_ms=st.clock.ms() - tc)}
        try:
            await asyncio.wait_for(st.speech_event.wait(), a.speech_wait)
            target = st.speech_after_call_ms + a.disconnect_after * 1000
            why = f"{a.disconnect_after}s after first model audio chunk after toolCall"
        except TimeoutError:
            target = st.clock.ms()
            why = f"fallback: no model speech within {a.speech_wait}s after toolCall"
            st.emit("disconnect_fallback", reason=why)
    await sleep_until(st, target)
    await drop_connection(st, why)
    if not await reconnect(st, "scripted_disconnect"):
        return
    await after_resume(st)


async def control_until_drop_point(st: RunState) -> None:
    """R0: "Hi, can you hear me?", wait for the model's reply to end and for a
    sessionResumptionUpdate after it (up to 6 s), then --disconnect-after s."""
    a = st.args
    hello = st.conn.mic.say("hello", a.clips["hello"])
    end = await hello.ended
    if end is None:
        return
    t0 = st.clock.ms()
    while st.clock.ms() - t0 < 15000:
        if (st.last_output_ms >= end and st.model_idle()
                and st.clock.ms() - st.last_output_ms >= 500):
            break
        await asyncio.sleep(0.05)
    reply_end = st.clock.ms()
    st.emit("control_reply_done", waited_ms=reply_end - t0,
            replied=st.last_output_ms >= end)
    t1 = st.clock.ms()
    while st.clock.ms() - t1 < 6000:
        if any(u["at_ms"] >= end for u in st.updates if u["conn"] == st.conn.n):
            break
        await asyncio.sleep(0.05)
    got = [u for u in st.updates if u["at_ms"] >= end and u["conn"] == st.conn.n]
    st.emit("control_update_after_reply", count=len(got),
            last=None if not got else {k: got[-1][k] for k in ("at_ms", "resumable",
                                                                 "handle_present")})
    await asyncio.sleep(a.disconnect_after)


async def after_resume(st: RunState, final: bool = True) -> None:
    a = st.args
    resumed_ms = st.reconnects[-1]["connected_ms"] if st.reconnects else st.clock.ms()
    # ---- after resume
    if a.old_response == "never":
        await sleep_until(st, resumed_ms + a.r2_ask_after * 1000)
        settle = f"{a.r2_ask_after}s after resume"
    else:
        try:
            await asyncio.wait_for(st.old_response_event.wait(), a.latency + 15)
        except TimeoutError:
            st.error("script", "old response was not sent")
        t_ref = (st.old_response or {}).get("sent_ms") or st.clock.ms()
        settle = await wait_ask_settled(st, t_ref)
    if not await ensure_connected(st):
        return
    await do_ask(st, "ask", final, settle)


async def do_ask(st: RunState, label: str, final: bool, settle: str | None) -> None:
    conn = st.conn
    st.emit("ask_queued", label=label, settle=settle, conn=conn.n, final=final)
    utt = conn.mic.say(label, st.args.clips["ask"])
    start = await utt.started
    end = await utt.ended
    rec = {"label": label, "start_ms": start, "end_ms": end, "conn": conn.n,
           "settle": settle, "final": final}
    st.asks.append(rec)
    st.ask = rec
    if end is None:
        st.error("script", f"{label} clip could not be sent")


# ------------------------------------------------------- round 2: network freeze --


def summarize_frame(data: dict) -> dict:
    """Compact, key-free description of one raw server frame."""
    out: dict[str, Any] = {"keys": sorted(data)}
    sc = data.get("serverContent")
    if isinstance(sc, dict):
        out["server_content"] = sorted(sc)
        audio = 0
        for part in (sc.get("modelTurn") or {}).get("parts") or []:
            audio += len(((part.get("inlineData") or {}).get("data")) or "")
        if audio:
            out["audio_b64_chars"] = audio
        for k in ("outputTranscription", "inputTranscription"):
            if isinstance(sc.get(k), dict) and sc[k].get("text"):
                out[k] = sc[k]["text"]
    tc = data.get("toolCall")
    if isinstance(tc, dict):
        out["tool_calls"] = [{"id": f.get("id"), "name": f.get("name"), "args": f.get("args")}
                             for f in tc.get("functionCalls") or []]
    for k in ("toolCallCancellation", "goAway", "error", "voiceActivity"):
        if k in data:
            out[k] = data[k]
    sru = data.get("sessionResumptionUpdate")
    if isinstance(sru, dict):
        out["resumption"] = {"resumable": sru.get("resumable"),
                             "handle_present": bool(sru.get("newHandle"))}
    return out


async def send_ping(st: RunState, conn: Conn) -> None:
    t = st.clock.ms()
    conn.pings_sent += 1
    try:
        waiter = await asyncio.wait_for(conn.raw_ws.ping(), 1.0)
        rtt = await asyncio.wait_for(waiter, 15.0)
        now = st.clock.ms()
        conn.last_rx_ms = max(conn.last_rx_ms, now)
        conn.pongs_ms.append((t, round(rtt * 1000, 1), now))
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def liveness(st: RunState, conn: Conn, act: bool) -> None:
    """Client-side loss detection. Rule: the link is lost when neither a server frame
    nor a WebSocket pong has arrived for --detect-after s; a ping goes out every
    --ping-every s so that a healthy but idle link always produces a pong."""
    a = st.args
    limit = a.detect_after * 1000
    next_ping = 0.0
    conn.last_rx_ms = max(conn.last_rx_ms, st.clock.ms())
    while not (conn.gone or conn.dropping or conn.suspended or st.ending):
        now = st.clock.ms()
        if now >= next_ping:
            next_ping = now + a.ping_every * 1000
            st.tasks.append(asyncio.create_task(send_ping(st, conn)))
        if now - conn.last_rx_ms >= limit:
            conn.lost_ms = now
            st.emit("link_lost_detected", conn=conn.n, silent_ms=now - conn.last_rx_ms,
                    last_rx_ms=conn.last_rx_ms,
                    since_freeze_ms=(now - st.freeze["at_ms"]) if st.freeze else None,
                    rule=f"no server frame and no pong for {a.detect_after}s, "
                         f"ping every {a.ping_every}s",
                    act=act, pings_sent=conn.pings_sent, pongs=len(conn.pongs_ms))
            if act:
                st.link_lost_event.set()
            return
        await asyncio.sleep(0.05)


async def suspend_connection(st: RunState, conn: Conn, why: str) -> None:
    """Stop using a connection without closing it: no more reads, no more writes."""
    conn.suspended = True
    await stop_conn_tasks(conn)
    st.flush_audio("link_lost")
    st.emit("connection_suspended", conn=conn.n, why=why,
            ws_state=str(getattr(conn.raw_ws, "state", None)),
            client_msg_index=conn.client_index,
            note="socket kept open; not read, not written, not closed")


async def drain_old(st: RunState, conn: Conn, t0: int | None = None,
                    label: str = "since_unfreeze_ms") -> None:
    """After the unfreeze (N1/N2), or from the blackhole on (B1-B3): read whatever the
    server sends on the old connection, straight from the websockets connection
    (nothing reaches the session logic)."""
    ws = conn.raw_ws
    if t0 is None:
        t0 = st.freeze.get("unfreeze_ms") or st.clock.ms()
    n = 0
    try:
        while True:
            raw = await ws.recv()
            n += 1
            t = st.clock.ms()
            try:
                data = json.loads(raw)
            except Exception:
                data = {"_unparseable": len(raw)}
            rec = {"conn": conn.n, label: t - t0, **summarize_frame(data)}
            st.old_frames.append(rec | {"at_ms": t})
            st.emit("old_conn_frame", **rec)
    except ConnectionClosed as exc:  # type: ignore[misc]
        t = st.clock.ms()
        rcvd, sent = getattr(exc, "rcvd", None), getattr(exc, "sent", None)
        st.old_close = {"at_ms": t, label: t - t0, "frames": n,
                        "rcvd_code": getattr(rcvd, "code", None),
                        "rcvd_reason": redact(str(getattr(rcvd, "reason", "") or ""))[:300],
                        "sent_code": getattr(sent, "code", None),
                        "rcvd_then_sent": getattr(exc, "rcvd_then_sent", None),
                        "exception": type(exc).__name__,
                        "cause": err_text(exc.__cause__) if exc.__cause__ else None}
        conn.gone = True
        st.emit("old_conn_closed", conn=conn.n, **st.old_close)
    except asyncio.CancelledError:
        st.emit("old_conn_drain_stopped", conn=conn.n, frames=n,
                ws_state=str(getattr(ws, "state", None)), **{label: st.clock.ms() - t0})
        raise


async def freeze_flow(st: RunState, tc: int) -> None:
    a = st.args
    conn1 = st.conn
    await sleep_until(st, tc + a.disconnect_after * 1000)
    last = st.updates[-1] if st.updates else None
    rtts = sorted(r for _, r, _ in conn1.pongs_ms)
    st.proxy.freeze(conn1.tunnel_id)
    fz = st.emit("freeze_start", conn=conn1.n, tunnel=conn1.tunnel_id,
                 since_tool_call_ms=st.clock.ms() - tc, pending_call_ids=st.pending_call_ids(),
                 model_idle=st.model_idle(), pings_sent=conn1.pings_sent,
                 pongs=len(rtts), pong_rtt_median_ms=rtts[len(rtts) // 2] if rtts else None,
                 pong_rtt_max_ms=rtts[-1] if rtts else None,
                 last_rx_ms=conn1.last_rx_ms, client_msg_index=conn1.client_index)
    st.freeze = {"at_ms": fz, "tunnel": conn1.tunnel_id, "pongs_before": len(rtts),
                 "rtt_median": rtts[len(rtts) // 2] if rtts else None,
                 "rtt_max": rtts[-1] if rtts else None}
    st.disconnect = {"start_ms": fz, "abort_ms": fz, "conn": conn1.n, "why": "freeze",
                     "last_update": last, "pending": st.pending_call_ids(),
                     "model_idle": st.model_idle(),
                     "usable_handle_ms": st.usable_handle["at_ms"] if st.usable_handle else None}
    try:
        await asyncio.wait_for(st.link_lost_event.wait(), a.detect_after + 10)
    except TimeoutError:
        st.error("liveness", "loss not detected within detect_after + 10 s")
    st.freeze["detect_ms"] = conn1.lost_ms
    await suspend_connection(st, conn1, "link lost (client rule)")
    if a.scenario == "N3":
        await sleep_until(st, fz + a.n3_unfreeze_after * 1000)
        await n3_unfreeze_and_probe(st, conn1)
        return
    if a.resume_after_freeze is not None:
        await sleep_until(st, fz + a.resume_after_freeze * 1000)
    st.resume_phase = "while_frozen"
    ok = await reconnect(st, "link_lost")
    ref = st.reconnects[-1]["connected_ms"] if ok else st.clock.ms()
    first = asyncio.create_task(after_resume(st, final=False)) if ok else None
    if first is not None:
        st.tasks.append(first)
    await sleep_until(st, ref + a.unfreeze_after_resume * 1000)
    st.proxy.unfreeze(conn1.tunnel_id)
    uf = st.emit("unfreeze", conn=conn1.n, tunnel=conn1.tunnel_id, since_freeze_ms=st.clock.ms() - fz,
                 since_resume_ms=(st.clock.ms() - ref) if ok else None,
                 old_ws_state=str(getattr(conn1.raw_ws, "state", None)))
    st.freeze["unfreeze_ms"] = uf
    st.tasks.append(asyncio.create_task(drain_old(st, conn1)))
    if first is None:
        await recover_after_unfreeze(st, conn1)
        return
    try:
        await first
    except Exception as exc:
        st.error("first_ask", err_text(exc))
    ask1_end = (st.asks[0].get("end_ms") if st.asks else None) or st.clock.ms()
    await sleep_until(st, max(uf + a.second_ask_after_unfreeze * 1000, ask1_end + 4000))
    t_wait = st.clock.ms()
    while st.clock.ms() - t_wait < 8000:
        if st.model_idle() and st.clock.ms() - st.last_output_ms >= 1000:
            break
        await asyncio.sleep(0.05)
    if not await ensure_connected(st):
        return
    await do_ask(st, "ask_after_unfreeze", True, f"{a.second_ask_after_unfreeze}s after unfreeze")


async def recover_after_unfreeze(st: RunState, conn1: Conn) -> None:
    """N1/N2 when no resume worked while frozen: the old path works again, so watch the
    old connection for --observe-old s, close it cleanly (close frame 1000 now reaches
    the server), then resume as in round 1 and finish the scenario on the new session."""
    await asyncio.sleep(st.args.observe_old)
    await close_old_then_resume(st, conn1)


async def close_old_then_resume(st: RunState, conn1: Conn) -> None:
    """Close the old connection cleanly (SDK context exit: close 1000), then resume with
    the round-1 schedule and finish the scenario on the new connection (N1/N2, B3)."""
    a = st.args
    t_close = st.emit("close_old_start", conn=conn1.n, old_gone=conn1.gone,
                      frames_since_unfreeze=len(st.old_frames),
                      ss=ss_flow(st.flow["local"], st.flow["remote"]) if st.flow else None)
    err = None
    if not conn1.gone and not conn1.exited:
        try:
            await asyncio.wait_for(conn1.cm.__aexit__(None, None, None), 10)
        except Exception as exc:
            err = err_text(exc)
        conn1.exited = True
        conn1.gone = True
    ws = conn1.raw_ws
    st.recovery = {"close_ms": t_close, "close_done_ms": st.clock.ms(),
                   "local_close_code": getattr(ws, "close_code", None),
                   "close_reason": str(getattr(ws, "close_reason", "") or "")[:200],
                   "error": err}
    st.emit("close_old_done", conn=conn1.n, **st.recovery)
    st.freeze["resume_while_frozen"] = st.resume_failed
    st.resume_failed = None
    st.resume_phase = "after_closing_old"
    saved = a.reconnect_retries
    a.reconnect_retries = 4
    ok = await reconnect(st, "after_closing_old_connection")
    a.reconnect_retries = saved
    if ok:
        st.recovery["resumed_ms"] = st.reconnects[-1]["connected_ms"]
        await after_resume(st, final=True)


async def n3_unfreeze_and_probe(st: RunState, conn: Conn) -> None:
    """N3: never resumed. Unfreeze the old tunnel, read what the server sends on it
    (SDK receiver, every frame logged), and if it is still open after --n3-observe s,
    use it again: send the pending call's response, then ask."""
    a = st.args
    st.proxy.unfreeze(conn.tunnel_id)
    uf = st.emit("unfreeze", conn=conn.n, tunnel=conn.tunnel_id,
                 since_freeze_ms=st.clock.ms() - st.freeze["at_ms"],
                 old_ws_state=str(getattr(conn.raw_ws, "state", None)))
    st.freeze["unfreeze_ms"] = uf
    conn.log_frames_from_ms = uf
    conn.suspended = False
    conn.recv_task = asyncio.create_task(receiver(st, conn))
    st.tasks.append(conn.recv_task)
    await asyncio.sleep(a.n3_observe)
    if conn.gone:
        st.old_close = dict(conn.close_info or {}) | {
            "since_unfreeze_ms": (conn.close_info or {}).get("at_ms", st.clock.ms()) - uf}
        st.emit("n3_old_conn_closed", **st.old_close)
        return
    conn.mic = Mic(st, conn)
    conn.mic_task = asyncio.create_task(conn.mic.run())
    conn.liveness_task = asyncio.create_task(liveness(st, conn, act=False))
    st.tasks += [conn.mic_task, conn.liveness_task]
    st.emit("n3_old_conn_reused", conn=conn.n, since_unfreeze_ms=st.clock.ms() - uf,
            frames_since_unfreeze=len(st.old_frames))
    st.resumed_event.set()   # releases the held response onto st.conn (the old connection)
    await after_resume(st, final=True)


# ------------------------------------------------- round 3: real packet loss --


def since_bh_ms(st: RunState, ts_wall: float) -> int:
    return int(round((ts_wall - st.bh_info["t_wall"]) * 1000))


async def blackhole_flow(st: RunState, tc: int) -> None:
    """B1/B2/B3: blackhole the live flow (iptables, both ways), detect the loss with the
    round-2 rule, keep the old socket open and unused, and resume at detection, then
    every --bh-period s, until one is accepted or --bh-max s (B3: the rule is removed
    after --b3-unblock-after s; after --b3-after-unblock s more without success, the
    old connection is closed and the round-1 schedule is used)."""
    a = st.args
    got = await blackhole_until_detect(st, tc)
    if got is None:
        return
    conn1, local, remote, t_bh = got
    first = conn1.lost_ms or st.clock.ms()
    if a.scenario == "B3":
        unblock_ms = t_bh + a.b3_unblock_after * 1000
        st.tasks.append(asyncio.create_task(b3_unblock(st, unblock_ms, local, remote, conn1)))
        deadline = unblock_ms + a.b3_after_unblock * 1000
    else:
        deadline = t_bh + a.bh_max * 1000
    ok = await resume_schedule(st, first, a.bh_period, deadline)
    if ok:
        await after_resume(st, final=True)
        return
    if st.resume_failed and st.resume_failed.startswith("quota"):
        return
    if a.scenario == "B3":
        st.emit("b3_close_old", since_blackhole_ms=st.clock.ms() - t_bh,
                attempts=len(st.resume_attempts))
        await close_old_then_resume(st, conn1)
        return
    st.resume_failed = (f"lockout: no resume accepted in {a.bh_max:.0f} s after the blackhole "
                        f"({len(st.resume_attempts)} attempts)")
    st.emit("lockout", since_blackhole_ms=st.clock.ms() - t_bh,
            attempts=len(st.resume_attempts), old_ws_state=str(getattr(conn1.raw_ws, "state", None)),
            ss=ss_flow(local, remote), counters=st.bh.counters())


async def blackhole_until_detect(st: RunState, tc: int) -> tuple | None:
    """Shared by B1-B3 and BR1/BR2: watch the live flow, wait for the drop point (B2/BR2:
    the follow-up question, then --disconnect-after s after the first model audio chunk;
    otherwise --disconnect-after s after the toolCall), blackhole it, wait for the
    client rule to fire, suspend the old connection (open, unused) and start reading it
    raw. Returns (conn1, local, remote, t_bh), or None if the flow cannot be handled."""
    a = st.args
    conn1 = st.conn
    tr = conn1.raw_ws.transport
    local = tuple(tr.get_extra_info("sockname")[:2])
    remote = tuple(tr.get_extra_info("peername")[:2])
    if ":" in local[0] or ":" in remote[0]:
        st.error("blackhole", f"IPv6 flow {local} -> {remote}: only IPv4 is handled")
        return None
    st.flow = {"local": list(local), "remote": list(remote)}

    def on_packet(p: dict) -> None:
        if st.bh_info is None:          # before the blackhole: count only
            st.bh_pkts_before[p["dir"]] += 1
            return
        rec = {"since_blackhole_ms": since_bh_ms(st, p["ts"]),
               **{k: p[k] for k in ("dir", "flags", "seq", "seq_end", "ack", "win", "len")}}
        st.bh_pkts.append(rec)
        st.emit("old_flow_packet", **rec)

    st.watch = FlowWatch(local, remote, a.bh_iface, on_packet)
    banner = await st.watch.start()
    st.bh.prepare()   # chains ready now, so that add() is only the two inserts
    st.emit("flow_watch_started", local=list(local), remote=list(remote), iface=a.bh_iface,
            banner=banner[:160])
    why = f"{a.disconnect_after}s after toolCall"
    if a.scenario in ("B2", "BR2"):   # R4 timing: follow-up question, model speaking
        await sleep_until(st, tc + a.followup_after_tool_call * 1000)
        conn1.mic.say("followup", a.clips["followup"])
        st.followup = {"queued_ms": st.emit("followup_queued",
                                            since_tool_call_ms=st.clock.ms() - tc)}
        try:
            await asyncio.wait_for(st.speech_event.wait(), a.speech_wait)
            target = st.speech_after_call_ms + a.disconnect_after * 1000
            why = f"{a.disconnect_after}s after first model audio chunk after toolCall"
        except TimeoutError:
            target = st.clock.ms()
            why = f"fallback: no model speech within {a.speech_wait}s after toolCall"
            st.emit("disconnect_fallback", reason=why)
    else:
        target = tc + a.disconnect_after * 1000
    await sleep_until(st, target)
    last = st.updates[-1] if st.updates else None
    rtts = sorted(r for _, r, _ in conn1.pongs_ms)
    ss0 = ss_flow(local, remote)
    model_idle = st.model_idle()
    info = st.bh.add(local, remote)
    t_bh = st.clock.ms()
    st.bh_info = info | {"at_ms": t_bh}
    # What the user had heard: model audio received so far plays out until play_end_ms
    # (it may end after the blackhole); the user's own last utterance ended earlier.
    st.loss_audio = {"play_end_ms": round(st.play_end_ms) if st.play_end_ms >= 0 else None,
                     "user_end_ms": max([u for u in st.user_audio_ends if u <= t_bh],
                                        default=None),
                     "last_model_chunk_ms": st.last_audio_chunk_ms}
    st.emit("blackhole_start", conn=conn1.n, why=why, rules=info["rules"],
            iptables=info["iptables"], since_tool_call_ms=t_bh - tc,
            pending_call_ids=st.pending_call_ids(), model_idle=model_idle,
            last_output_ms=st.last_output_ms, pings_sent=conn1.pings_sent, pongs=len(rtts),
            pong_rtt_median_ms=rtts[len(rtts) // 2] if rtts else None,
            last_rx_ms=conn1.last_rx_ms, client_msg_index=conn1.client_index,
            packets_before=dict(st.bh_pkts_before), ss_before=ss0)
    st.freeze = {"at_ms": t_bh, "pongs_before": len(rtts),
                 "rtt_median": rtts[len(rtts) // 2] if rtts else None,
                 "rtt_max": rtts[-1] if rtts else None, "model_idle": model_idle}
    st.disconnect = {"start_ms": t_bh, "abort_ms": t_bh, "conn": conn1.n, "why": "blackhole",
                     "last_update": last, "pending": st.pending_call_ids(),
                     "model_idle": model_idle,
                     "usable_handle_ms": st.usable_handle["at_ms"] if st.usable_handle else None}
    st.tasks.append(asyncio.create_task(bh_monitor(st, local, remote, conn1)))
    try:
        await asyncio.wait_for(st.link_lost_event.wait(), a.detect_after + 10)
    except TimeoutError:
        st.error("liveness", "loss not detected within detect_after + 10 s")
    st.freeze["detect_ms"] = conn1.lost_ms
    await suspend_connection(st, conn1, "link lost (client rule)")
    # Frames already in the client kernel before the rule are read just after t_bh: the
    # audio the user got is complete only now (nothing is read from conn1 after this).
    st.loss_audio["play_end_ms_at_detection"] = (round(st.play_end_ms)
                                                 if st.play_end_ms >= 0 else None)
    st.tasks.append(asyncio.create_task(drain_old(st, conn1, t0=t_bh,
                                                  label="since_blackhole_ms")))
    return conn1, local, remote, t_bh


# ------------------------------------------- stage 2: client-side recovery --


async def recovery_flow(st: RunState, tc: int) -> None:
    """BR1/BR2: the same blackhole and detection as B1/B2, then recovery.py: resume
    window with the handle and a close sent on the old socket in parallel; if no resume
    is accepted, a new session restored from the client's ledger. Then, once the model
    has reacted (or stayed silent), ask "Did you book it?"."""
    a = st.args
    got = await blackhole_until_detect(st, tc)
    if got is None:
        return
    conn1, local, remote, t_bh = got
    h = st.usable_handle

    async def connect(handle: str | None) -> Any:
        st.resume_phase = "resume_window" if handle else "new_session"
        conn = await open_connection(st, handle)
        return conn.session

    async def close_old() -> dict:
        """SDK context exit on the old connection: websockets sends a close frame (1000),
        waits close_timeout (10 s, not overridden by the SDK) for the handshake, then
        aborts the transport. On a dead path neither the close nor the RST leaves."""
        ws = conn1.raw_ws
        err = None
        try:
            await asyncio.wait_for(conn1.cm.__aexit__(None, None, None), a.close_old_wait)
        except Exception as exc:
            err = err_text(exc)
            try:
                ws.transport.abort()
            except Exception:
                pass
        conn1.exited = True
        conn1.gone = True
        proto = getattr(ws, "protocol", None)
        sent = getattr(proto, "close_sent", None)
        rcvd = getattr(proto, "close_rcvd", None)
        return {"close_sent": getattr(sent, "code", None),
                "close_rcvd": getattr(rcvd, "code", None),
                "close_rcvd_reason": redact(str(getattr(rcvd, "reason", "") or ""))[:200],
                "ws_state": str(getattr(ws, "state", None)), "sdk_exit_error": err,
                "counters_out": st.bh.counters().get("out") if st.bh else None}

    mode = await st.rec.recover(h["value"] if h else None, connect, close_old)
    if mode == "resumed":
        c = st.conn
        rec = {"reason": "recovery_resume_window", "phase": "resume_window", "conn": c.n,
               "start_ms": conn1.lost_ms, "attempts": len(st.rec.attempts),
               "first_raw_ms": c.first_raw_ms, "connected_ms": c.connected_ms,
               "first_raw_has_setup_complete": c.first_raw_has_setup_complete,
               "first_raw_keys": c.first_raw_keys, "handle_at_ms": h["at_ms"],
               "handle_conn": h["conn"],
               "abort_to_first_raw_ms": (c.first_raw_ms - t_bh) if c.first_raw_ms else None,
               "abort_to_connected_ms": c.connected_ms - t_bh, "blackhole_active": st.bh.active}
        st.reconnects.append(rec)
        st.emit("resumed", **rec)
        st.resumed_event.set()
        await after_resume(st, final=True)
        return
    if mode != "new_session":
        st.resume_failed = f"recovery {mode}: {st.rec.fatal or 'no session could be opened'}"
        if st.rec.fatal:
            st.error("quota", st.rec.fatal)
        else:
            st.error("recovery", st.resume_failed)
        return
    if st.rec.restore and st.rec.restore.get("error"):
        st.error("restore", st.rec.restore["error"])
    t_ref = st.rec.restore["sent_ms"] if st.rec.restore else st.clock.ms()
    settle = await wait_ask_settled(st, t_ref)
    if st.conn is None or st.conn.gone:
        st.error("script", "the recovered session closed before the ask")
        return
    await do_ask(st, "ask", True, f"after restore: {settle}")


async def resume_schedule(st: RunState, first_ms: int, period_s: float, deadline_ms: float
                          ) -> bool:
    """Resume attempts with the last handle, started at first_ms + k * period_s (an
    attempt that overruns its slot delays the next one); no attempt starts after
    deadline_ms."""
    a = st.args
    h = st.usable_handle
    t_bh = st.bh_info["at_ms"]
    st.emit("reconnect_start", reason="link_lost",
            schedule=f"at detection, then every {period_s}s, none after "
                     f"+{(deadline_ms - t_bh) / 1000:.0f}s",
            handle_sha=h["sha"] if h else None, handle_from_ms=h["at_ms"] if h else None,
            handle_before_tool_call=(h["at_ms"] < st.tool_call_at_ms)
            if h and st.tool_call_at_ms is not None else None)
    if h is None:
        st.resume_failed = "no resumable handle received before the blackhole"
        st.error("reconnect", st.resume_failed)
        return False
    k = 0
    while True:
        target = first_ms + k * period_s * 1000
        if target > deadline_ms:
            return False
        await sleep_until(st, target)
        k += 1
        st.resume_phase = "blackholed" if st.bh.active else "path_back_old_open"
        t_try = st.clock.ms()
        try:
            conn = await asyncio.wait_for(open_connection(st, h["value"]), a.attempt_timeout)
        except Exception as exc:
            failed = st.conns[-1] if st.conns else None
            hung = isinstance(exc, TimeoutError)
            if hung:
                if failed is not None:
                    failed.gone = True
                exc = TimeoutError(f"no setupComplete within {a.attempt_timeout}s")
            rec_fail = {"attempt": k, "phase": st.resume_phase, "start_ms": t_try,
                        "since_blackhole_ms": t_try - t_bh, "since_abort_ms": t_try - t_bh,
                        "error": err_text(exc),
                        "ms_to_error": st.clock.ms() - t_try, "hung": hung,
                        "setup_sent": bool(failed and failed.frames_sent),
                        "server_frames": failed.frames_recv if failed else None,
                        "close": failed.close_info if failed else None,
                        "attempt_ws_state": str(getattr(getattr(failed, "raw_ws", None),
                                                        "state", None))}
            st.resume_attempts.append(rec_fail)
            st.emit("reconnect_attempt_failed", **rec_fail)
            text = rec_fail["error"] + " " + json.dumps(rec_fail["close"] or {})
            if QUOTA_RE.search(text):
                st.resume_failed = f"quota/billing error: {text[:400]}"
                st.error("quota", text[:400])
                return False
            continue
        rec = {"reason": "link_lost", "phase": st.resume_phase, "conn": conn.n,
               "start_ms": first_ms, "attempt_start_ms": t_try, "attempts": k,
               "first_raw_ms": conn.first_raw_ms, "connected_ms": conn.connected_ms,
               "first_raw_has_setup_complete": conn.first_raw_has_setup_complete,
               "first_raw_keys": conn.first_raw_keys, "handle_at_ms": h["at_ms"],
               "handle_conn": h["conn"],
               "abort_to_first_raw_ms": (conn.first_raw_ms - t_bh)
               if conn.first_raw_ms is not None else None,
               "abort_to_connected_ms": conn.connected_ms - t_bh,
               "since_blackhole_ms": conn.connected_ms - t_bh,
               "blackhole_active": st.bh.active}
        st.reconnects.append(rec)
        st.emit("resumed", **rec)
        st.resumed_event.set()
        return True


async def b3_unblock(st: RunState, at_ms: float, local: tuple, remote: tuple,
                     conn1: Conn) -> None:
    await sleep_until(st, at_ms)
    info = await asyncio.to_thread(st.bh.remove)
    t = st.clock.ms()
    st.bh_info["unblock_ms"] = t
    st.bh_info["unblock_wall"] = info["t_wall"]
    st.freeze["unfreeze_ms"] = t
    st.emit("blackhole_removed", since_blackhole_ms=t - st.bh_info["at_ms"],
            removed=info["removed"], counters=await asyncio.to_thread(st.bh.counters),
            ss=await asyncio.to_thread(ss_flow, local, remote),
            old_ws_state=str(getattr(conn1.raw_ws, "state", None)))


async def bh_monitor(st: RunState, local: tuple, remote: tuple, conn1: Conn) -> None:
    """Every second: iptables counters of the two chains (logged on change), the client
    socket as ss shows it (logged on change of state/retrans/backoff/unacked/timer kind,
    and every --ss-every s), and the old websocket's state."""
    a = st.args
    t_bh = st.bh_info["at_ms"]
    last_cnt = last_key = last_ws = None
    last_ss_ms = -10 ** 9
    try:
        while not st.ending:
            now = st.clock.ms()
            cnt = await asyncio.to_thread(st.bh.counters)
            if cnt != last_cnt:
                rec = {"since_blackhole_ms": now - t_bh, "counters": cnt, "active": st.bh.active}
                st.bh_counters.append(rec | {"at_ms": now})
                st.emit("bh_counters", **rec)
                last_cnt = cnt
            ss = await asyncio.to_thread(ss_flow, local, remote)
            key = (ss.get("state"), ss.get("retrans"), ss.get("backoff"), ss.get("unacked"),
                   (ss.get("timer") or "").split(",")[0])
            if key != last_key or now - last_ss_ms >= a.ss_every * 1000:
                rec = {"since_blackhole_ms": now - t_bh, **ss}
                st.bh_ss.append(rec | {"at_ms": now})
                st.emit("old_flow_ss", **rec)
                last_key, last_ss_ms = key, now
            ws_state = str(getattr(conn1.raw_ws, "state", None))
            if ws_state != last_ws:
                st.emit("old_ws_state", since_blackhole_ms=now - t_bh, state=ws_state)
                last_ws = ws_state
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        st.error("bh_monitor", err_text(exc))


async def wait_until_done(st: RunState, script_task: asyncio.Task) -> str:
    a = st.args
    while True:
        if st.ask is None or st.ask.get("end_ms") is None or not st.ask.get("final", True):
            if script_task.done():
                if st.no_tool_call:
                    return "no_tool_call"
                if st.resume_failed:
                    return "resume_failed"
                exc = None if script_task.cancelled() else script_task.exception()
                if exc is not None:
                    st.error("script", err_text(exc))
                return "script_ended_without_ask"
            await asyncio.sleep(0.05)
            continue
        ref = st.ask["end_ms"]
        now = st.clock.ms()
        if st.conn is None or st.conn.gone:
            return "ws_closed_after_ask"
        if now - ref >= a.post_ask_window * 1000:
            return "post_ask_window_elapsed"
        if now - ref >= a.min_post_ask * 1000:
            jobs_done = all(t.done() for t in st.service.jobs.values())
            answered = st.last_output_ms >= ref
            last = max([st.last_output_ms, ref] + [r["at_ms"] for r in st.tool_responses])
            if jobs_done and answered and st.model_idle() and now - last >= a.quiet * 1000:
                return "settled"
        await asyncio.sleep(0.05)


# ----------------------------------------------------------------- one run --


QUOTA_RE = re.compile(r"quota|billing|RESOURCE_EXHAUSTED|exceeded your current|prepay|"
                      r"credits?\b|payment|\b429\b", re.IGNORECASE)
EXIT_QUOTA = 3


async def run_once(args: argparse.Namespace, run: int, out: JsonlWriter, client: Any
                   ) -> RunState:
    st = RunState(args=args, run=run, clock=Clock(), out=out, client=client)
    st.wall = datetime.now().isoformat(timespec="seconds")
    if args.proxy_obj is not None:
        st.proxy = args.proxy_obj

        def on_proxy_event(name: str, **fields: Any) -> None:
            t = st.emit(f"proxy_{name}", **fields)
            st.proxy_events.append({"at_ms": t, "name": name} | fields)
        st.proxy.on_event = on_proxy_event
    st.bh = args.bh_obj
    if args.scenario.startswith("BR"):
        st.rec = Recovery(
            RecoveryConfig(ping_every_s=args.ping_every, detect_after_s=args.detect_after,
                           resume_window_s=args.resume_window, resume_every_s=args.resume_every,
                           close_old=not args.no_close_old,
                           close_old_timeout_s=args.close_old_wait,
                           summary_turns=args.summary_turns,
                           status_notes=not args.no_status_note),
            clock_ms=st.clock.ms, log=lambda event, **kw: st.emit(event, **kw),
            key_fn=business_key, is_fatal=lambda text: bool(QUOTA_RE.search(text)),
            model_idle=st.model_idle)
    st.emit("run_start", wall=st.wall, model=args.model, sdk=SDK_VERSION,
            stage2=None if st.rec is None else {
                k: v for k, v in vars(st.rec.cfg).items() if not k.startswith("note")
                and k != "cut_marker"} | {"status_notes": st.rec.cfg.status_notes},
            round3=None if not args.scenario.startswith("B") else {
                "blackhole": "iptables DROP of the live 4-tuple, OUTPUT and INPUT",
                "iptables": st.bh.version, "websockets_keepalive": "ping_interval=None",
                "proxy": None, "detect_rule": f"no server frame and no pong for "
                                              f"{args.detect_after}s, ping every {args.ping_every}s",
                "resume": f"at detection, then every {args.bh_period}s, up to {args.bh_max}s",
                "b3_unblock_after_s": args.b3_unblock_after,
                "b3_after_unblock_s": args.b3_after_unblock},
            scenario=args.scenario, disconnect_after_s=args.disconnect_after,
            latency_s=args.latency, old_response=args.old_response,
            inject_transparent=args.inject_transparent, drop_mode=args.drop_mode,
            resume_delay_s=args.resume_delay,
            round2=None if not args.scenario.startswith("N") else {
                "proxy": "freeze_proxy.py HTTP CONNECT on 127.0.0.1",
                "websockets_keepalive": "ping_interval=None",
                "detect_rule": f"no server frame and no pong for {args.detect_after}s, "
                               f"ping every {args.ping_every}s",
                "resume_after_freeze_s": args.resume_after_freeze,
                "unfreeze_after_resume_s": args.unfreeze_after_resume,
                "n3_unfreeze_after_s": args.n3_unfreeze_after,
                "reconnect_retries": args.reconnect_retries},
            clips_s={k: round(pcm_seconds(v), 3) for k, v in args.clips.items()},
            chunk_ms=int(CHUNK_S * 1000), mime_type=AUDIO_MIME,
            **({"save_audio": True} if getattr(args, "save_audio", False) else {}),
            **({"voice_dir": args.voice_dir} if args.voice_dir != DEFAULT_VOICE_DIR else {}))
    TAP.inject_transparent = args.inject_transparent
    script_task: asyncio.Task | None = None
    try:
        async with asyncio.timeout(args.run_timeout):
            await open_connection(st, None)
            script_task = asyncio.create_task(script(st))
            st.done_reason = await wait_until_done(st, script_task)
            st.emit("run_stop_condition", reason=st.done_reason)
    except TimeoutError:
        st.done_reason = "run_timeout"
        st.error("run_timeout", f"hard timeout {args.run_timeout}s")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        st.done_reason = "exception"
        st.error("session", err_text(exc))
    finally:
        st.ending = True
        if script_task is not None:
            script_task.cancel()
        if st.rec is not None:
            await st.rec.aclose()
        for conn in st.conns:
            await stop_conn_tasks(conn)
        for t in st.tasks:
            t.cancel()
        await asyncio.gather(*st.tasks, *([script_task] if script_task else []),
                             return_exceptions=True)
        pending = [j for j, t in st.service.jobs.items() if not t.done()]
        if pending:
            st.emit("jobs_pending_at_run_end", job_ids=pending)
        for t in st.service.jobs.values():
            t.cancel()
        await asyncio.gather(*st.service.jobs.values(), return_exceptions=True)
        if st.watch is not None:
            await st.watch.stop()
        if st.bh is not None and st.bh_info is not None:
            try:   # final view of the old flow, then remove the rules and chains
                fin = {"counters": st.bh.counters(), "rules_active": st.bh.active,
                       "ss": ss_flow(st.flow["local"], st.flow["remote"]),
                       "old_ws_state": str(getattr(st.conns[0].raw_ws, "state", None)),
                       "since_blackhole_ms": st.clock.ms() - st.bh_info["at_ms"],
                       "tcpdump_unparsed": st.watch.unparsed if st.watch else None}
                st.emit("blackhole_final", **fin)
                st.bh_info["final"] = fin
            except Exception as exc:
                st.error("blackhole_final", err_text(exc))
            try:
                st.bh.cleanup()
                st.emit("blackhole_cleaned_up")
            except Exception as exc:
                st.error("blackhole_cleanup", err_text(exc))
        conn = st.conn
        if conn is not None and not conn.gone and conn.cm is not None and not conn.exited:
            try:
                await asyncio.wait_for(conn.cm.__aexit__(None, None, None), 5)
                conn.exited = True
                st.emit("session_closed_cleanly", conn=conn.n)
            except Exception as exc:
                st.emit("session_close_error", conn=conn.n, detail=err_text(exc))
        for c in st.conns:   # round 2: old connections left open by design
            if c is conn or c.exited or c.session is None:
                continue
            if c.tunnel_id is not None and st.proxy is not None:
                t = st.proxy.tunnels.get(c.tunnel_id)
                if t is not None and t.frozen:
                    st.proxy.unfreeze(c.tunnel_id)
            try:
                c.raw_ws.transport.abort()
            except Exception:
                pass
            try:
                await asyncio.wait_for(c.cm.__aexit__(None, None, None), 3)
            except Exception:
                pass
            c.exited = True
            st.emit("old_conn_aborted_at_run_end", conn=c.n)
        if st.proxy is not None:
            for c in st.conns:
                if c.tunnel_id is not None:
                    st.proxy.close_tunnel(c.tunnel_id)
        st.flush_audio("run_end")
    return st


# ----------------------------------------------------------------- summary --

_CLAUSE_SPLIT = re.compile(r"[.;:!?,]|\b(?:and|but|so|because|although|however)\b")
_NEGATION = re.compile(r"\b(?:not|no|never|nothing|cannot)\b|n't\b")
_BOOK_WORD = re.compile(r"\b(?:book|booked|made|make|schedule|scheduled|reserve|reserved|"
                        r"confirm|confirmed)\b")
_CANCEL_STATE = re.compile(r"\b(?:cancell?ed|stopped|called off)\b")
_BOOKED_STATE = re.compile(r"\b(?:booked|made|scheduled|reserved|confirmed|all set)\b")
_SUBORDINATE = re.compile(r"\b(?:before|after|when|until|since|once)\b")
Claim = Literal["claims_booked", "claims_not_booked", "neither"]


def classify_claim(text: str) -> Claim:
    """Keyword rules from ../gemini-live-commit-guard/commit_guard.py TruthCheck (the
    last clause with a claim wins). A first pass only: FINDINGS.md reviews every
    answer by hand."""
    def one(part: str) -> Claim:
        negated = bool(_NEGATION.search(part))
        if negated and _BOOK_WORD.search(part):
            return "claims_not_booked"
        if not negated and _CANCEL_STATE.search(part):
            return "claims_not_booked"
        if not negated and _BOOKED_STATE.search(part):
            return "claims_booked"
        return "neither"
    claim: Claim = "neither"
    norm = re.sub(r"\s+", " ", text.replace("’", "'").lower())
    for clause in _CLAUSE_SPLIT.split(norm):
        if not clause or not clause.strip():
            continue
        sub = _SUBORDINATE.search(clause)
        c = one(clause[:sub.start()] if sub else clause)
        if c == "neither" and sub:
            c = one(clause[sub.start():])
        if c != "neither":
            claim = c
    return claim


def join_turns(chunks: list[tuple[int, int, int, str]]) -> str:
    turns: dict[int, str] = {}
    for _, _, turn, txt in chunks:
        turns[turn] = turns.get(turn, "") + txt
    return " / ".join(re.sub(r"\s+", " ", v).strip() for v in turns.values() if v.strip())


def md(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|").strip()


CLIP_SOURCES = {"book_request": "book.wav", "followup": "bring.wav", "hello": "hello.wav"}


def save_run_audio(st: RunState, out_dir: Path) -> dict:
    """--save-audio: <name>_run<N>_model.wav (every model audio chunk of the run, all
    connections, concatenated in arrival order) and <name>_run<N>_audio.json (each
    chunk's arrival time, connection and offset in the WAV; each user clip's send times
    and source file in assets/audio/). Same layout as ../gemini-live-stop-test."""
    a = st.args
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{a.name}_run{st.run}"
    rates = {c["rate"] for c in st.audio_out}
    if len(rates) > 1:
        raise ValueError(f"model audio at several rates: {sorted(rates)}")
    rate = rates.pop() if rates else OUT_AUDIO_RATE
    chunks, offset = [], 0
    for meta, data in zip(st.audio_out, st.audio_data):
        chunks.append({"t_ms": meta["t_ms"], "conn": meta["conn"], "turn": meta["turn"],
                       "bytes": len(data), "wav_offset_ms": round(offset / 2 / rate * 1000, 1),
                       "duration_ms": round(len(data) / 2 / rate * 1000, 1)})
        offset += len(data)
    pcm = b"".join(st.audio_data)
    if len(pcm) % 2:
        pcm = pcm[:-1]
    wav_path = out_dir / f"{stem}_model.wav"
    if pcm:
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
    user = []
    for c in st.clip_sends:
        src = CLIP_SOURCES.get(c["label"], "did_you_book.wav")
        user.append(c | {"source": f"{a.voice_dir}/{src}", "sample_rate": AUDIO_RATE,
                         "duration_ms": round(pcm_seconds(load_pcm(a.voice_path / src)) * 1000, 1)})
    sidecar = {
        "scenario": a.name, "run": st.run, "wall": st.wall, "model": a.model,
        "time_base": "ms since session start, the t_ms of the JSONL. t_ms of a model chunk "
                     "is when the harness received it; sent_start_ms / sent_end_ms of a "
                     "user clip are when its first / last 100 ms chunk was sent.",
        "model_audio": {
            "file": wav_path.name if pcm else None, "sample_rate": rate, "channels": 1,
            "sample_width_bits": 16, "duration_s": round(len(pcm) / 2 / rate, 3),
            "chunks": len(chunks),
            "note": "Chunks are concatenated in arrival order with no gaps. The server "
                    "sends audio faster than real time, so wav_offset_ms is not the "
                    "arrival time: place each chunk at its t_ms or later. A client that "
                    "plays audio drops what is still queued when `interrupted` arrives; "
                    "this file keeps everything received.",
        },
        "chunks": chunks,
        "user_clips": user,
        "events": {"interrupted_ms": st.interrupted_ms,
                   "generation_complete_ms": st.gen_complete_ms,
                   "turn_complete_ms": st.turn_complete_ms},
    }
    side_path = out_dir / f"{stem}_audio.json"
    side_path.write_text(redact(json.dumps(sidecar, indent=1, ensure_ascii=False, default=str)),
                         encoding="utf-8")
    return {"model_wav": wav_path.name if pcm else None, "sidecar": side_path.name,
            "model_audio_s": sidecar["model_audio"]["duration_s"], "sample_rate": rate}


def summarize(st: RunState) -> dict:
    tc = st.tool_call_at_ms
    d = st.disconnect or {}
    last = d.get("last_update") or {}
    rec = st.reconnects[0] if st.reconnects else {}
    old_ids = {c["id"] for c in st.calls if c["old"]}
    reissued = [c for c in st.calls if c["after_resume"] and c["name"] == "book_slot"]
    first_key = st.first_call["key"] if st.first_call else None
    if reissued:
        reissue = "; ".join(
            f"yes: {c['id']} args={json.dumps(c['args'])} same key={'yes' if c['key'] == first_key else 'no'}"
            f" same id={'yes' if c['id'] in old_ids else 'no'}"
            f" (+{c['at_ms'] - (rec.get('connected_ms') or 0)} ms after resume)" for c in reissued)
    else:
        reissue = "no"
    resume_ms = rec.get("connected_ms")
    ask = (st.asks[0] if st.asks else st.ask) or {}
    ask_start = ask.get("start_ms")
    between = join_turns([x for x in st.texts if resume_ms is not None and x[0] >= resume_ms
                          and (ask_start is None or x[0] < ask_start)])
    answer = join_turns([x for x in st.texts if ask_start is not None and x[0] >= ask_start])
    before = join_turns([x for x in st.texts if d.get("abort_ms") is None
                         or x[0] < d["abort_ms"]])
    commits = len(st.service.committed)
    claim = classify_claim(answer) if answer else "neither"
    consistent = (claim == "claims_booked") == (commits >= 1) and claim != "neither"
    o = st.old_response
    if st.args.old_response == "never":
        old = "not sent (scenario)"
    elif o is None:
        old = "not sent"
    else:
        post = [e for e in st.server_errors if o.get("sent_ms") and e["at_ms"] >= o["sent_ms"]]
        closed = [c for c in st.conns[1:] if c.gone and c.close_info and not c.dropping
                  and o.get("sent_ms") is not None
                  and c.close_info.get("at_ms", -1) >= o["sent_ms"]]
        bits = [f"sent @{o.get('sent_ms')} on conn {o.get('conn')}"]
        if o.get("error"):
            bits.append(f"send error: {o['error']}")
        if post:
            bits.append("server error after: " + json.dumps(post[0]["error"])[:160])
        if closed:
            bits.append(f"conn {closed[0].n} closed code={closed[0].close_info.get('code')} "
                        f"{closed[0].close_info.get('reason', '')[:120]}")
        sent = o.get("sent_ms")
        react = join_turns([x for x in st.texts if sent is not None and x[0] >= sent
                            and (ask_start is None or x[0] < ask_start)])
        bits.append(f"model before ask: \"{react}\"" if react else "no model output before ask")
        if o.get("confirmation_id") and o["confirmation_id"].split("-")[-1] in answer:
            bits.append(f"answer cites {o['confirmation_id']}")
        old = "; ".join(bits)
    upd1 = [u for u in st.updates if u["conn"] == 1]
    upd2 = [u for u in st.updates if u["conn"] > 1]
    row = {
        "run": st.run,
        "wall": st.wall,
        "tool_call_ms": tc if tc is not None else "no_tool_call",
        "call": (f"{st.first_call['id']} {json.dumps(st.first_call['args'])}"
                 if st.first_call else "-"),
        "disconnect_ms": (f"{d['abort_ms']} (+{d['abort_ms'] - tc} vs call)"
                          if d and tc is not None else f"{d['abort_ms']}" if d else "-"),
        "updates_conn1": len(upd1),
        "last_update_before_drop": (f"@{last.get('at_ms')} resumable={last.get('resumable')} "
                                    f"handle={'yes' if last.get('handle_present') else 'no'}"
                                    if last else "none"),
        "handle_used": (f"from @{rec.get('handle_at_ms')} "
                        f"({rec['handle_at_ms'] - tc:+d} ms vs call)"
                        if rec.get("handle_at_ms") is not None and tc is not None
                        else f"from @{rec.get('handle_at_ms')}" if rec.get("handle_at_ms") is not None
                        else (f"from @{st.usable_handle['at_ms']} (resume failed)"
                              if st.usable_handle else "none")),
        "resume_attempts": (f"{len(st.resume_attempts) + (1 if rec else 0)} "
                            f"({len(st.resume_attempts)} failed"
                            + (": " + "; ".join(f"#{x['attempt']} +{x['since_abort_ms']} ms "
                                               f"{x['error'][:70]}" for x in st.resume_attempts)
                               if st.resume_attempts else "") + ")"),
        "reconnect_ms": (f"{rec.get('abort_to_first_raw_ms')} to first msg, "
                         f"{rec.get('abort_to_connected_ms')} to connect() return"
                         if rec else (st.resume_failed or "-")),
        "new_setup_complete": ("yes" if rec.get("first_raw_has_setup_complete")
                               else "no" if rec else "-"),
        "session_id_same": ("-" if not rec or not st.conns[0].session_id_sha
                            else "yes" if st.conns[0].session_id_sha
                            == st.conns[rec["conn"] - 1].session_id_sha else "no"),
        "updates_after_resume": len(upd2),
        "reissued_call": reissue,
        "old_response": old,
        "model_before_drop": before[:300],
        "model_after_resume_before_ask": between[:300] or "-",
        "answer": answer[:300] or "-",
        "commits": commits,
        "commit_ids": ", ".join(f"{c['confirmation_id']}<-{c['call_id']}"
                                for c in st.service.committed) or "-",
        "claim": claim,
        "consistent": "yes" if consistent else "no",
        "go_away": len(st.go_aways),
        "cancellations": len(st.cancellations),
        "interrupted": len(st.interrupted_ms),
        "done_reason": st.done_reason,
        "errors": "; ".join(e[:200] for e in st.errors) or "-",
        "input_transcripts": [f"{t}@c{c}: {x}" for t, c, x in st.input_texts],
        "last_consumed_idx_seen": sorted({u["last_consumed_client_message_index"]
                                          for u in st.updates
                                          if u["last_consumed_client_message_index"] is not None}),
    }
    if st.args.scenario.startswith("N"):
        row = summarize_freeze(st, row)
    if st.args.scenario.startswith("B"):
        row = summarize_blackhole(st, row)
    if st.rec is not None:
        row = summarize_recovery(st, row)
    return row


def summarize_recovery(st: RunState, row: dict) -> dict:
    """Stage-2 columns (BR1/BR2): what recovery.py did and what the user got."""
    r = st.rec
    tl = r.timeline
    t_bh = (st.bh_info or {}).get("at_ms")
    det = tl.get("detected_ms")

    def rel(t: Any, ref: Any) -> str:
        return f"+{(t - ref) / 1000:.2f} s" if t is not None and ref is not None else "-"
    groups: dict[str, list[dict]] = {}
    for x in r.attempts:
        groups.setdefault("accepted" if x.get("accepted") else x["error"][:70], []).append(x)
    att = "; ".join(
        f"{len(v)} x {k} (at {', '.join(rel(x['start_ms'], det) for x in v)} after detection"
        + (f"; {min(x['ms_to_error'] for x in v)} to {max(x['ms_to_error'] for x in v)} ms each"
           if k != "accepted" else "") + ")" for k, v in groups.items()) or "none"
    co = r.close_old_info or {}
    old_close = (f"close sent={co.get('close_sent')} rcvd={co.get('close_rcvd')}, done "
                 f"{rel(co.get('done_ms'), co.get('start_ms'))} after start, ws state "
                 f"{co.get('ws_state')}, out counters {co.get('counters_out')}"
                 if co else "not attempted / not finished")
    fresh = st.conn if r.mode in ("new_session", "resumed") else None
    if r.mode == "new_session" and fresh is not None:
        new = (f"setupComplete {rel(fresh.first_raw_ms, t_bh)} after the blackhole, "
               f"{rel(fresh.first_raw_ms, det)} after detection, "
               f"{rel(fresh.first_raw_ms, tl.get('window_end_ms'))} after the window end "
               f"(conn {fresh.n}, {len(r.new_session_tries)} try)")
    elif r.mode == "resumed" and fresh is not None:
        new = f"RESUMED: setupComplete {rel(fresh.first_raw_ms, t_bh)} after the blackhole"
    else:
        new = f"{r.mode}: {r.fatal or '-'}"
    rs = r.restore or {}
    restore = ("summary: " + " | ".join(f"{t['role']}: {t['text']}" for t in rs.get("summary", []))
               + f" || note: {rs.get('note')}" if rs else "-")
    sent = rs.get("sent_ms")
    ask = (st.asks[0] if st.asks else st.ask) or {}
    ask_start = ask.get("start_ms")
    after = join_turns([x for x in st.texts if sent is not None and x[0] >= sent
                        and (ask_start is None or x[0] < ask_start)])
    dd = "; ".join(f"{d['call_id']} {json.dumps(d['args'])} -> {d['original_call_id']} "
                   f"({d['state']}, +{d['at_ms'] - (sent or 0)} ms after restore)"
                   for d in r.dedupes) or "none"
    reissued = [c for c in st.calls if c["after_resume"] and c["name"] == "book_slot"]
    la = st.loss_audio or {}
    play = la.get("play_end_ms_at_detection", la.get("play_end_ms"))
    heard = max([v for v in (play, la.get("user_end_ms")) if v is not None], default=None)
    which = ("model audio playback" if heard is not None and heard == play
             else "end of the user's utterance")
    first_new = st.first_audio_by_conn.get(fresh.n) if fresh is not None else None
    gap = (f"{(first_new - heard) / 1000:.2f} s (last heard @{heard}: {which}; first new audio "
           f"@{first_new}; {rel(first_new, t_bh)} after the blackhole)"
           if first_new is not None and heard is not None else
           f"no model audio in the recovered session (last heard @{heard})")
    row.update({
        "resume_window": att,
        "old_close": old_close,
        "new_session": new,
        "restore": restore,
        "model_after_restore": after or "-",
        "dedupe": (f"{len(reissued)} re-issued: " + dd) if reissued else "no re-issued call",
        "gap": gap,
        "recovery_mode": r.mode,
        "update_notes": "; ".join(n["text"] for n in r.notes) or "-",
    })
    return row


def describe_segments(pkts: list[dict]) -> str:
    """Server segments on the blackholed flow: time, flags, length, and whether the
    sequence range was already seen (a retransmission)."""
    if not pkts:
        return "none"
    base: dict[str, int] = {}
    top: dict[str, int] = {}
    out = []
    for p in pkts:
        d, tag = p["dir"], ""
        if p["seq"] is not None:   # sequence offsets per direction
            base.setdefault(d, p["seq"])
            end = p["seq_end"] if p["seq_end"] is not None else p["seq"]
            if d in top and end <= top[d] and p["len"]:
                tag = " retx"
            top[d] = max(top.get(d, end), end)
            tag = f" seq+{p['seq'] - base[d]}{tag}"
        out.append(f"+{p['since_blackhole_ms'] / 1000:.2f}s {d} [{p['flags']}] {p['len']}B{tag}")
    return "; ".join(out)


def summarize_blackhole(st: RunState, row: dict) -> dict:
    """Round-3 columns (B1/B2/B3)."""
    b = st.bh_info or {}
    f = st.freeze or {}
    tc, t_bh, det = st.tool_call_at_ms, b.get("at_ms"), f.get("detect_ms")
    unblock_rel = (b["unblock_ms"] - t_bh) if b.get("unblock_ms") and t_bh is not None else None

    def outcome(x: dict) -> str:
        c = x.get("close") or {}
        if x.get("hung"):
            return f"hung (no setupComplete in {st.args.attempt_timeout:.0f} s)"
        if c.get("code") is not None:
            return f"close {c.get('code')} {c.get('reason', '')}".strip()
        return x["error"][:60]
    groups: dict[tuple, list] = {}
    for x in st.resume_attempts:
        rel = x.get("since_blackhole_ms", x.get("since_abort_ms"))
        groups.setdefault((x.get("phase"), outcome(x)), []).append((rel, x["ms_to_error"]))
    att = "; ".join(f"{len(v)} x {k[1]} [{k[0]}], at +{v[0][0] / 1000:.1f} to "
                    f"+{v[-1][0] / 1000:.1f} s, {min(m for _, m in v)} to "
                    f"{max(m for _, m in v)} ms each" for k, v in groups.items())
    acc = st.reconnects[0] if st.reconnects else None
    if acc:
        first_ok = (f"+{(acc['connected_ms'] - t_bh) / 1000:.1f} s after the blackhole "
                    f"(attempt {acc['attempts']} of phase {acc.get('phase')}, "
                    f"rule {'still active' if acc.get('blackhole_active') else 'removed'})")
    else:
        first_ok = st.resume_failed or "none"
    during = [p for p in st.bh_pkts if p["since_blackhole_ms"] >= 0
              and (unblock_rel is None or p["since_blackhole_ms"] < unblock_rel)]
    s2c = [p for p in during if p["dir"] == "s2c"]
    c2s = [p for p in during if p["dir"] == "c2s"]
    fin = b.get("final") or {}
    cnt = fin.get("counters") or (st.bh_counters[-1]["counters"] if st.bh_counters else {})
    srv = (f"{len(s2c)} segments seen while blackholed: {describe_segments(s2c)[:900]}; "
           f"counters in {cnt.get('in')}")
    ss_states = []
    for r in st.bh_ss:
        k = f"{r.get('state')} timer={r.get('timer')} backoff={r.get('backoff')} unacked={r.get('unacked')}"
        if not ss_states or ss_states[-1][1] != k:
            ss_states.append((r["since_blackhole_ms"], k))
    cli = (f"{len(c2s)} client segments on the wire while blackholed (out counters "
           f"{cnt.get('out')}); socket: "
           + "; ".join(f"+{t / 1000:.0f}s {k}" for t, k in ss_states[:8]))
    oc = st.old_close
    old = (f"{len(st.old_frames)} frames received"
           + (f"; closed at +{oc.get('since_blackhole_ms', 0) / 1000:.1f} s: rcvd "
              f"{oc.get('rcvd_code')} \"{oc.get('rcvd_reason')}\" ({oc.get('exception')}"
              f"{', ' + oc['cause'] if oc.get('cause') else ''})" if oc else
              f"; still {fin.get('old_ws_state')} at run end"))
    if unblock_rel is not None:
        after = [p for p in st.bh_pkts if p["since_blackhole_ms"] >= unblock_rel]
        old += (f"; after the rule was removed (+{unblock_rel / 1000:.1f} s): "
                + describe_segments(after[:16])[:600]
                + "; frames: " + ", ".join(
                    f"+{fr['since_blackhole_ms'] / 1000:.1f}s {'/'.join(fr['keys'])}"
                    for fr in st.old_frames[:10]))
    rc = st.recovery
    row.update({
        "blackhole_ms": f"{t_bh} (+{t_bh - tc} vs call)" if t_bh is not None and tc is not None else "-",
        "model_at_blackhole": ("idle" if f.get("model_idle") else "speaking") if f else "-",
        "detect": f"+{det - t_bh} ms" if det is not None and t_bh is not None else "not detected",
        "resume_attempts_b": f"{len(st.resume_attempts)} failed: {att}" if att else "0 failed",
        "first_accepted_resume": first_ok,
        "server_on_old_flow": srv,
        "client_on_old_flow": cli,
        "old_conn": old,
        "recovery": (f"old conn closed +{(rc['close_ms'] - t_bh) / 1000:.1f} s after the "
                     f"blackhole (local close code {rc['local_close_code']}, "
                     f"{rc['close_done_ms'] - rc['close_ms']} ms); "
                     + (f"resumed {rc['resumed_ms'] - rc['close_done_ms']} ms later"
                        if rc.get("resumed_ms") else f"resume failed: {st.resume_failed}"))
        if rc else "-",
    })
    return row


def summarize_freeze(st: RunState, row: dict) -> dict:
    """Round-2 columns (N1/N2/N3)."""
    f = st.freeze or {}
    tc, fz, det, uf = st.tool_call_at_ms, f.get("at_ms"), f.get("detect_ms"), f.get("unfreeze_ms")
    tun = f.get("tunnel")
    pe = [e for e in st.proxy_events if e.get("tunnel") == tun]

    def server_side(e: dict) -> dict:
        return ((e.get("tcp") or {}).get("server_side") or {})
    fr = next((e for e in pe if e["name"] == "freeze"), None)
    ufe = next((e for e in pe if e["name"] == "unfreeze"), None)
    tcp = []
    for e in pe:
        if e["name"] == "tcp_state_change" and fz is not None:
            tcp.append(f"+{e['at_ms'] - fz} ms: {e['before']} -> {e['after']}")
    if fr and ufe and server_side(fr) and server_side(ufe):
        a0, a1 = server_side(fr), server_side(ufe)
        tcp.append(f"at unfreeze server side {a1.get('state')}, kernel got "
                   f"{a1.get('rxbytes', 0) - a0.get('rxbytes', 0)} B from server during "
                   f"freeze, {a1.get('snd_sbbytes')} B queued to server")
    for e in pe:
        if e["name"] in ("eof", "pump_error") and uf is not None:
            tcp.append(f"{e['name']} {e.get('direction')} +{e['at_ms'] - uf} ms after unfreeze"
                       + (f" ({e.get('detail')})" if e.get("detail") else ""))
    kinds: dict[str, int] = {}
    for fr_ in st.old_frames:
        k = "/".join(fr_["keys"]) + (("[" + ",".join(fr_["server_content"]) + "]")
                                     if fr_.get("server_content") else "")
        kinds[k] = kinds.get(k, 0) + 1
    texts = " ".join(x for fr_ in st.old_frames for x in
                     [fr_.get("outputTranscription"), fr_.get("inputTranscription")] if x)
    oc = st.old_close
    old_after = (f"{len(st.old_frames)} frames {kinds}" if st.old_frames else "0 frames")
    if texts:
        old_after += f"; transcripts: \"{texts[:200]}\""
    if oc:
        old_after += (f"; closed +{oc.get('since_unfreeze_ms')} ms after unfreeze: "
                      f"code {oc.get('rcvd_code', oc.get('code'))} "
                      f"\"{oc.get('rcvd_reason', oc.get('reason', ''))}\"")
    else:
        old_after += "; not closed by the server before run end"
    answers = []
    for i, ask in enumerate(st.asks):
        if ask.get("start_ms") is None:
            continue
        end = st.asks[i + 1]["start_ms"] if i + 1 < len(st.asks) else None
        answers.append(join_turns([x for x in st.texts if x[0] >= ask["start_ms"]
                                   and (end is None or x[0] < end)]))
    commits = len(st.service.committed)

    def cons(ans: str) -> str:
        c = classify_claim(ans) if ans else "neither"
        return "yes" if (c == "claims_booked") == (commits >= 1) and c != "neither" else "no"
    new_conn = None
    if uf is not None and st.reconnects:
        nc = st.conns[st.reconnects[-1]["conn"] - 1]
        bad = []
        if nc.gone and nc.close_info and nc.close_info.get("at_ms", 0) >= uf:
            bad.append(f"closed code {nc.close_info.get('code')}")
        bad += [f"interrupted @{t}" for t in st.interrupted_ms if t >= uf
                and not any(a_["start_ms"] and a_["start_ms"] <= t <= a_["start_ms"] + 1500
                            for a_ in st.asks)]
        bad += [f"server error {e['error']}" for e in st.server_errors if e["at_ms"] >= uf]
        bad += [f"toolCall {c['id']}" for c in st.calls if c["at_ms"] >= uf]
        bad += [f"cancellation {c['ids']}" for c in st.cancellations if c["at_ms"] >= uf]
        new_conn = "; ".join(bad) or "nothing unusual"
    row.update({
        "freeze_ms": f"{fz} (+{fz - tc} vs call)" if fz is not None and tc is not None else "-",
        "detect": (f"+{det - fz} ms after freeze" if det is not None and fz is not None
                   else "not detected"),
        "pongs_before_freeze": (f"{f.get('pongs_before')} pongs, rtt median "
                                f"{f.get('rtt_median')} ms, max {f.get('rtt_max')} ms"),
        "old_tcp": "; ".join(tcp) or "-",
        "unfreeze": f"{uf} (+{uf - fz} vs freeze)" if uf is not None and fz is not None else "-",
        "old_conn_after_unfreeze": old_after,
        "answer": answers[0] if answers else "-",
        "answer_after_unfreeze": answers[1] if len(answers) > 1 else "-",
        "consistent": "/".join(cons(x) for x in answers) or "-",
        "new_conn_after_unfreeze": new_conn or "-",
        "resume_from_freeze": (f"{st.reconnects[0]['abort_to_first_raw_ms']} ms freeze -> "
                               f"setupComplete, {st.reconnects[0]['attempts']} attempt(s)"
                               if st.reconnects else (st.resume_failed or "no resume")),
    })
    frozen = [x for x in st.resume_attempts if x.get("phase") == "while_frozen"]
    ok_frozen = [r for r in st.reconnects if r.get("phase") == "while_frozen"]
    if st.args.scenario in ("N1", "N2"):
        errs = sorted({x["error"][:60] for x in frozen})
        row["resume_while_frozen"] = (
            (f"succeeded +{ok_frozen[0]['first_raw_ms'] - fz} ms after freeze, "
             f"{ok_frozen[0]['attempts']} attempt(s)") if ok_frozen else
            f"{len(frozen)} attempts at +" + ", +".join(f"{(x['start_ms'] - fz) / 1000:.1f}"
                                                         for x in frozen)
            + f" s after freeze, all failed: {errs}")
    rc = st.recovery
    if rc:
        after = [r for r in st.reconnects if r.get("phase") == "after_closing_old"]
        fails = [x for x in st.resume_attempts if x.get("phase") == "after_closing_old"]
        row["recovery"] = (
            f"old conn closed by client +{rc['close_ms'] - (uf or 0)} ms after unfreeze "
            f"(local close code {rc['local_close_code']}); "
            + (f"resumed {after[0]['first_raw_ms'] - rc['close_done_ms']} ms after the close, "
               f"{after[0]['attempts']} attempt(s)"
               + (f" ({len(fails)} failed: {sorted({x['error'][:40] for x in fails})})"
                  if fails else "")
               if after else f"resume failed: {st.resume_failed}"))
    else:
        row["recovery"] = "-"
    return row


COLUMNS_N = ["run", "tool_call_ms", "freeze_ms", "detect", "resume_while_frozen",
             "old_tcp", "unfreeze", "old_conn_after_unfreeze", "recovery", "reissued_call",
             "old_response", "answer", "new_conn_after_unfreeze", "answer_after_unfreeze",
             "commits", "consistent", "done_reason"]


COLUMNS = ["run", "tool_call_ms", "disconnect_ms", "last_update_before_drop", "handle_used",
           "resume_attempts", "reconnect_ms", "new_setup_complete", "reissued_call", "old_response",
           "answer", "commits", "claim", "consistent", "go_away", "errors"]


COLUMNS_B = ["run", "tool_call_ms", "blackhole_ms", "model_at_blackhole", "detect",
             "resume_attempts_b", "first_accepted_resume", "server_on_old_flow",
             "client_on_old_flow", "old_conn", "recovery", "reissued_call", "old_response",
             "answer", "commits", "consistent", "done_reason", "errors"]


COLUMNS_BR = ["run", "tool_call_ms", "blackhole_ms", "model_at_blackhole", "detect",
              "resume_window", "old_close", "new_session", "restore", "model_before_drop",
              "model_after_restore", "dedupe", "answer", "commits", "consistent", "gap",
              "update_notes", "server_on_old_flow", "done_reason", "errors"]


def summary_columns(scenario: str) -> list[str]:
    return (COLUMNS_N if scenario.startswith("N")
            else COLUMNS_BR if scenario.startswith("BR")
            else COLUMNS_B if scenario.startswith("B") else COLUMNS)


def summary_header(cfg: dict, when: str) -> str:
    """cfg uses the key names of the run_start event (make_summary.py passes those)."""
    drop = f"drop_mode={cfg['drop_mode']}, " if cfg.get("drop_mode") else ""
    voice = f", user clips from {cfg['voice_dir']}" if cfg.get("voice_dir") else ""
    return (f"model `{cfg['model']}`, google-genai {cfg['sdk']}, {when}, scenario "
            f"{cfg['scenario']}, disconnect_after={cfg['disconnect_after_s']}s, "
            f"latency={cfg['latency_s']}s (prepare, then commit at once, no guard), "
            f"old_response={cfg['old_response']}, inject_transparent={cfg['inject_transparent']}, "
            f"{drop}speech input (16 kHz PCM, 100 ms chunks, real time{voice}). Times are ms "
            f"since session start.")


def summary_block(title: str, header: str, cols: list[str], rows: list[dict]) -> str:
    lines = [f"### {title}", "", header, "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(md(r.get(c, "-")) for c in cols) + " |")
    return "\n".join(lines) + "\n\n"


def append_summary(path: Path, args: argparse.Namespace, rows: list[dict]) -> str:
    """Live path: append this invocation's rows. make_summary.py rebuilds the whole file
    from the run_end events of the JSONL files with the same three functions."""
    cfg = {"model": args.model, "sdk": SDK_VERSION, "scenario": args.scenario,
           "disconnect_after_s": args.disconnect_after, "latency_s": args.latency,
           "old_response": args.old_response, "inject_transparent": args.inject_transparent,
           "drop_mode": args.drop_mode}
    if args.voice_dir != DEFAULT_VOICE_DIR:
        cfg["voice_dir"] = args.voice_dir
    block = summary_block(args.name, summary_header(cfg, datetime.now().strftime("%Y-%m-%d %H:%M")),
                          summary_columns(args.scenario), rows)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return block


# -------------------------------------------------------------------- main --


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Gemini Live API: in-flight tool call vs. dropped connection + resume.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--scenario", choices=sorted(SCENARIO_DEFAULTS), required=True)
    p.add_argument("--name", default=None, help="results/<name>.jsonl (default: scenario)")
    p.add_argument("-n", "--runs", type=int, default=3)
    p.add_argument("--first-run", type=int, default=1,
                   help="number of the first run (to continue a scenario in a second call)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--disconnect-after", type=float, default=None,
                   help="R1/R2: s after toolCall; R3: s after commit; R4: s after the first "
                        "model audio chunk after toolCall (default per scenario)")
    p.add_argument("--latency", type=float, default=None,
                   help="prepare time of the fake service in s (default 4.0, R4 7.0)")
    p.add_argument("--r2-ask-after", type=float, default=2.0,
                   help="R2: ask this many s after the resumed connect() returns")
    p.add_argument("--followup-after-tool-call", type=float, default=0.5)
    p.add_argument("--speech-wait", type=float, default=10.0,
                   help="R4: max s to wait for model speech after toolCall")
    p.add_argument("--ask-min-after-response", type=float, default=3.0)
    p.add_argument("--ask-max-wait", type=float, default=10.0)
    p.add_argument("--min-post-ask", type=float, default=4.0)
    p.add_argument("--post-ask-window", type=float, default=15.0)
    p.add_argument("--quiet", type=float, default=3.0)
    p.add_argument("--tool-call-wait", type=float, default=15.0)
    p.add_argument("--run-timeout", type=float, default=90.0)
    p.add_argument("--between-runs", type=float, default=3.0)
    p.add_argument("--max-reconnects", type=int, default=2)
    p.add_argument("--reconnect-retries", type=int, default=4,
                   help="extra resume attempts after a failed one, after retry-base x 1, 2, 4, "
                        "... s (all with the same handle, all inside the same run)")
    p.add_argument("--retry-base", type=float, default=1.0)
    p.add_argument("--retry-fixed", type=float, default=None,
                   help="round 2 probe: retry every this many s while frozen (instead of "
                        "doubling)")
    p.add_argument("--attempt-timeout", type=float, default=10.0,
                   help="max s for one resume attempt to reach setupComplete")
    p.add_argument("--resume-delay", type=float, default=0.0,
                   help="seconds between the drop and the first resume attempt")
    g = p.add_argument_group("round 2 (N1/N2/N3, network freeze)")
    g.add_argument("--detect-after", type=float, default=2.0,
                   help="client rule: link lost after this many s without a server frame "
                        "or a pong")
    g.add_argument("--ping-every", type=float, default=0.5)
    g.add_argument("--resume-after-freeze", type=float, default=None,
                   help="first resume attempt this many s after the freeze (default: at "
                        "detection; N2: 5.0)")
    g.add_argument("--unfreeze-after-resume", type=float, default=10.0)
    g.add_argument("--second-ask-after-unfreeze", type=float, default=3.0)
    g.add_argument("--n3-unfreeze-after", type=float, default=30.0)
    g.add_argument("--n3-observe", type=float, default=3.0,
                   help="N3: s to only read the old connection after the unfreeze")
    g.add_argument("--observe-old", type=float, default=8.0,
                   help="N1/N2 without a resume: s to watch the old connection after the "
                        "unfreeze before closing it and resuming")
    g3 = p.add_argument_group("round 3 (B1/B2/B3, real packet loss, Linux container with "
                              "NET_ADMIN)")
    g3.add_argument("--bh-period", type=float, default=10.0,
                    help="resume attempts at detection, then every this many s")
    g3.add_argument("--bh-max", type=float, default=900.0,
                    help="B1/B2: no resume attempt starts later than this many s after the "
                         "blackhole")
    g3.add_argument("--b3-unblock-after", type=float, default=30.0,
                    help="B3: remove the rule this many s after the blackhole")
    g3.add_argument("--b3-after-unblock", type=float, default=60.0,
                    help="B3: keep trying with the old connection open this many s after "
                         "the rule is removed, then close it and resume")
    g3.add_argument("--bh-iface", default="eth0")
    g3.add_argument("--ss-every", type=float, default=30.0,
                    help="log the client socket (ss) at least this often")
    g4 = p.add_argument_group("stage 2 (BR1/BR2, client-side recovery with recovery.py)")
    g4.add_argument("--resume-window", type=float, default=4.0,
                    help="s after detection during which resume attempts may start")
    g4.add_argument("--resume-every", type=float, default=1.0,
                    help="s between resume attempt starts inside the window")
    g4.add_argument("--summary-turns", type=int, default=6,
                    help="transcript turns restored in the new session")
    g4.add_argument("--no-status-note", action="store_true",
                    help="ablation: restore the summary only, no side-effect status lines")
    g4.add_argument("--no-close-old", action="store_true",
                    help="do not send a close on the old socket at detection")
    g4.add_argument("--close-old-wait", type=float, default=15.0,
                    help="max s for the close attempt on the old socket")
    p.add_argument("--fake-live", default=None,
                   help="offline dry run: ws:// URL of a local fake Live server (use with a "
                        "dummy GEMINI_API_KEY and a scratch --results-dir)")
    p.add_argument("--drop-mode", choices=["abort", "clean"], default="abort",
                   help="abort: transport.abort(), no close frame (default); clean: normal "
                        "WebSocket close (code 1000) at the same point, as a control")
    p.add_argument("--budget", type=int, default=18,
                   help="max sessions over all invocations (counted in results/sessions.log)")
    p.add_argument("--inject-transparent", action="store_true",
                   help="add \"transparent\": true to setup.sessionResumption on the wire "
                        "(the SDK refuses it in Gemini Developer API mode)")
    p.add_argument("--save-audio", action="store_true",
                   help="write each run's model output audio to <results-dir>/audio_out/"
                        "<name>_run<N>_model.wav plus a JSON sidecar with chunk arrival "
                        "times and user clip send times (clip material; same run otherwise)")
    p.add_argument("--voice-dir", default=DEFAULT_VOICE_DIR,
                   help="folder of the user clips (book.wav, did_you_book.wav, bring.wav, "
                        "hello.wav), relative to this script. The default is the macOS `say` "
                        "voice of every published run; assets/audio/af_heart is the Kokoro-82M "
                        "af_heart voice")
    p.add_argument("--results-dir", default=str(HERE / "results"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    d_after, lat, old = SCENARIO_DEFAULTS[args.scenario]
    args.disconnect_after = d_after if args.disconnect_after is None else args.disconnect_after
    args.latency = lat if args.latency is None else args.latency
    args.old_response = old
    args.name = args.name or args.scenario
    args.voice_dir = Path(args.voice_dir).as_posix().rstrip("/")
    args.voice_path = HERE / args.voice_dir
    if args.scenario.startswith("N"):
        args.reconnect_retries = max(args.reconnect_retries, 5)   # 1, 2, 4, 8, 16 s
        args.run_timeout = max(args.run_timeout, 300.0)
        if args.retry_fixed:
            args.run_timeout = max(args.run_timeout,
                                   args.retry_fixed * (args.reconnect_retries + 1) + 120)
        if args.scenario == "N2" and args.resume_after_freeze is None:
            args.resume_after_freeze = 5.0
        if args.scenario == "N3":
            args.max_reconnects = 0
    if args.scenario.startswith("BR"):
        args.run_timeout = max(args.run_timeout, 150.0)
    elif args.scenario.startswith("B"):
        span = (args.b3_unblock_after + args.b3_after_unblock + 60 if args.scenario == "B3"
                else args.bh_max)
        args.run_timeout = max(args.run_timeout, span + 240.0)
    return args


def sessions_used(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


async def amain(args: argparse.Namespace) -> int:
    try:
        voice = args.voice_path
        args.clips = {"book": load_pcm(voice / "book.wav"),
                      "ask": load_pcm(voice / "did_you_book.wav")}
        if args.scenario in ("R4", "B2", "BR2"):
            args.clips["followup"] = load_pcm(voice / "bring.wav")
        if args.scenario == "R0":
            args.clips["hello"] = load_pcm(voice / "hello.wav")
    except Exception as exc:
        print(f"cannot load audio clips: {exc}", file=sys.stderr)
        return 2
    load_dotenv(HERE / ".env")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("GEMINI_API_KEY is not set (put it in .env next to this script).", file=sys.stderr)
        return 2
    _SECRETS.append(key)
    client = genai.Client(api_key=key, vertexai=False)
    TAP.fake_uri = args.fake_live
    args.proxy_obj = None
    args.bh_obj = None
    if args.scenario.startswith("B"):
        from blackhole import available
        why = available()
        if why:
            print(f"round 3 runs in a Linux container with --cap-add NET_ADMIN: {why}",
                  file=sys.stderr)
            return 2
        args.bh_obj = Blackhole()
        args.bh_obj.cleanup()   # leftovers from an interrupted run, if any
        TAP.direct_no_keepalive = True
    if args.scenario.startswith("N"):
        from freeze_proxy import FreezeProxy
        args.proxy_obj = await FreezeProxy({LIVE_HOST}).start()
        TAP.proxy_url = args.proxy_obj.url
    results = Path(args.results_dir)
    results.mkdir(parents=True, exist_ok=True)
    ledger = results / "sessions.log"
    out = JsonlWriter(results / f"{args.name}.jsonl", args.name, args.verbose)
    rows: list[dict] = []
    quota = None
    try:
        last_run = args.first_run + args.runs - 1
        for run in range(args.first_run, last_run + 1):
            used = sessions_used(ledger)
            if used >= args.budget:
                print(f"[{args.name}] budget reached: {used} sessions used of {args.budget}")
                break
            with ledger.open("a") as fh:
                fh.write(f"{datetime.now().isoformat(timespec='seconds')} {args.name} run {run}\n")
            try:
                st = await run_once(args, run, out, client)
                row = summarize(st)
                if args.save_audio:
                    try:
                        row["saved_audio"] = save_run_audio(st, results / "audio_out")
                    except Exception as exc:
                        row["saved_audio"] = {"error": err_text(exc)}
                        out.write(run, -1, "save_audio_failed", detail=err_text(exc))
                quota = next((e for e in st.errors if QUOTA_RE.search(e)), None)
            except Exception as exc:
                quota = err_text(exc) if QUOTA_RE.search(err_text(exc)) else None
                out.write(run, -1, "run_crashed", detail=err_text(exc))
                row = {c: "-" for c in COLUMNS} | {"run": run, "errors": err_text(exc)}
            out.write(run, -1, "run_end", summary=row)
            rows.append(row)
            print(redact(f"[{args.name} run {run}/{last_run}] call={row.get('tool_call_ms')} "
                         f"drop={row.get('disconnect_ms')} last_update="
                         f"{row.get('last_update_before_drop')} handle={row.get('handle_used')} "
                         f"reconnect={row.get('reconnect_ms')} setupComplete="
                         f"{row.get('new_setup_complete')} reissued={row.get('reissued_call')} "
                         f"old_response={row.get('old_response')} answer=\"{row.get('answer')}\" "
                         f"commits={row.get('commits')} consistent={row.get('consistent')} "
                         f"errors={row.get('errors')} done={row.get('done_reason')}"))
            if quota:
                out.write(run, -1, "stopped_on_quota_or_billing_error", detail=quota)
                print(redact(f"[{args.name}] stopping: quota/billing error: {quota}"))
                break
            if run < last_run:
                await asyncio.sleep(args.between_runs)
    finally:
        out.close()
        if args.proxy_obj is not None:
            await args.proxy_obj.stop()
    if rows:
        block = append_summary(results / "summary.md", args, rows)
        print()
        print(redact(block))
    return EXIT_QUOTA if quota else 0


def main() -> None:
    args = parse_args()
    try:
        sys.exit(asyncio.run(amain(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
