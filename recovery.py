"""recovery: client-side recovery for a Gemini Live session whose connection was lost
silently while a side-effecting tool call was pending.

Round 3 (FINDINGS.md) showed that after a real silent loss, with the old TCP flow dead
and no close reaching the server, every resume with the handle is refused with
`1011 Internal error encountered.` for at least 15 minutes, while the booking commits
and is never reported. This module is the client side of the fallback. It is a
reference pattern, not a package; resume_test.py (scenarios BR1 and BR2) drives it.

The four steps (the client owns the truth, not the model):

1. Detect the loss fast: a WebSocket ping every 0.5 s, the link is lost after 2.0 s
   without a server frame or a pong. The caller runs this rule (resume_test.liveness(),
   unchanged since round 2); RecoveryConfig only carries its two numbers.
2. Resume window: attempts with the last handle for `resume_window_s` (one at once,
   then one every `resume_every_s`, start to start; none starts after the window, and
   an attempt still open at the end of the window is abandoned). In parallel, a close
   for the old connection is sent on the old socket: on a dead path it never reaches
   the server, but it costs nothing, and on a live path it unlocks the resume at once.
3. Fallback: open a NEW session (no handle) and restore what the user needs from the
   client's own ledger, with one `send_client_content`:
   - the last `summary_turns` turns of the client's transcript (input and output
     transcription), as prior `user` and `model` turns; a model turn cut by the loss
     is marked as cut;
   - one user turn: a "System note" with one status line per side effect that is
     pending or finished in the last `recent_s`, taken from the service, not from the
     model.
   The pending call's id died with the old session: it is never answered. The side
   effect keeps running under the client's policy. If it finishes after the restore, a
   follow-up note is sent (when the model is idle). If the model issues the same call
   again, it is deduplicated by business key (the commit-guard key: tool name plus
   sorted JSON of the canonical arguments) and answered from the existing job.
4. Measure: the caller records the gap between the last audio heard before the loss
   and the first model audio in the recovered session.

No API key, handle or token goes through this module's logs: handles are passed in and
out but never logged.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from google.genai import types

FINAL_STATES = ("committed", "cancelled", "failed")


# ------------------------------------------------------------------- config --


@dataclass
class RecoveryConfig:
    # step 1 (run by the caller)
    ping_every_s: float = 0.5
    detect_after_s: float = 2.0
    # step 2
    resume_window_s: float = 4.0
    resume_every_s: float = 1.0
    attempt_timeout_s: float = 2.5      # also capped by the end of the window
    close_old: bool = True
    close_old_timeout_s: float = 15.0
    # step 3
    new_session_attempts: int = 2
    new_session_timeout_s: float = 10.0
    summary: bool = True
    summary_turns: int = 6
    status_notes: bool = True
    recent_s: float = 300.0             # finished effects older than this are not reported
    restore_turn_complete: bool = True  # the model speaks once restored
    dedupe: bool = True
    dedupe_window_s: float = 600.0      # longer than commit_guard's 30 s: a loss lasts
    update_note_idle_wait_s: float = 5.0
    cut_marker: str = " [cut off here by the connection loss]"
    note_intro: str = ("System note: the voice connection to the user was lost and the "
                       "conversation continues in a new session. The turns above are the "
                       "last turns before the loss, from the app's own transcript.")
    note_intro_no_summary: str = ("System note: the voice connection to the user was lost "
                                  "and the conversation continues in a new session.")
    note_committed: str = ("The {what} was confirmed at {time} during the connection loss. "
                           "Tell the user it is confirmed if they ask.")
    note_pending: str = ("The {what} is still being processed. It does not need to be "
                         "requested again. Tell the user it is in progress if they ask.")
    note_cancelled: str = "The {what} was not made: {detail}."
    note_outro: str = "Continue the conversation from where it stopped."
    note_update: str = ("System note: the {what} is now confirmed ({time}). Tell the user "
                        "it is confirmed.")


# ------------------------------------------------------------------- ledger --


@dataclass
class Turn:
    role: str            # "user" or "model"
    text: str
    at_ms: int
    closed: bool = False  # ended by turnComplete or interrupted
    cut: bool = False     # model turn open when the link was lost


class Transcript:
    """The client's own log of what was said, from input and output transcription.
    Consecutive chunks of one role form one turn; turnComplete or interrupted closes
    the open turn, so two user utterances separated by a model turn that had only a
    tool call stay two turns."""

    def __init__(self) -> None:
        self.turns: list[Turn] = []

    def add(self, role: str, text: str, at_ms: int) -> None:
        if not text:
            return
        last = self.turns[-1] if self.turns else None
        if last is not None and last.role == role and not last.closed:
            last.text += text
        else:
            self.turns.append(Turn(role, text, at_ms))

    def close_turn(self) -> None:
        if self.turns and not self.turns[-1].closed:
            self.turns[-1].closed = True

    def mark_loss(self) -> None:
        """At the loss: an open model turn was cut; any open turn is closed."""
        if self.turns and not self.turns[-1].closed:
            last = self.turns[-1]
            last.cut = last.role == "model"
            last.closed = True

    def last(self, n: int) -> list[Turn]:
        return [t for t in self.turns if t.text.strip()][-n:] if n > 0 else []


@dataclass
class Effect:
    """One side-effecting call, as the client and its service see it."""

    call_id: str
    name: str
    args: dict[str, Any]
    key: str
    started_ms: int
    session: int                      # 1 = first Live session, 2 = after one fallback
    state: str = "pending"            # pending -> committed | cancelled | failed
    result: dict[str, Any] | None = None
    finished_ms: int | None = None
    finished_wall: float | None = None
    detail: str = ""
    orphaned: bool = False            # its call id died with a lost session
    reported: str | None = None       # the state the model was last told
    duplicates: list[str] = field(default_factory=list)
    responded: set[str] = field(default_factory=set)


def sorted_json(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def default_key(name: str, args: dict[str, Any]) -> str:
    """commit_guard.GuardedTool.key with the identity normalisation; callers pass the
    tool's own canonical form (resume_test.business_key uses canonical_slot)."""
    return f"{name}:{sorted_json(args)}"


def describe_booking(args: dict[str, Any], result: dict[str, Any] | None) -> str:
    slot = str(args.get("slot", "the requested slot"))
    ref = (result or {}).get("confirmation_id")
    return f"booking for {slot} ({ref})" if ref else f"booking for {slot}"


# ----------------------------------------------------------------- recovery --


class Recovery:
    """Owns the client ledger of one conversation and the recovery after a loss.

    The caller feeds the ledger (transcript chunks, calls started, finished and
    answered) and, when its liveness rule fires, awaits `recover()`."""

    def __init__(self, cfg: RecoveryConfig | None = None, *,
                 clock_ms: Callable[[], int],
                 log: Callable[..., Any] | None = None,
                 key_fn: Callable[[str, dict[str, Any]], str] = default_key,
                 describe: Callable[[dict[str, Any], dict[str, Any] | None], str] = describe_booking,
                 is_fatal: Callable[[str], bool] | None = None,
                 model_idle: Callable[[], bool] | None = None) -> None:
        self.cfg = cfg or RecoveryConfig()
        self.ms = clock_ms
        self._log_cb = log
        self.key_fn = key_fn
        self.describe = describe
        self.is_fatal = is_fatal or (lambda _text: False)
        self.model_idle = model_idle or (lambda: True)
        self.transcript = Transcript()
        self.effects: list[Effect] = []
        self.session_no = 1
        self.session: Any = None          # the SDK session in use after recover()
        self.mode: str | None = None      # "resumed" | "new_session" | "failed" | "fatal"
        self.recovering = False
        self.recovered = asyncio.Event()
        self.attempts: list[dict[str, Any]] = []
        self.new_session_tries: list[dict[str, Any]] = []
        self.close_old_info: dict[str, Any] | None = None
        self.restore: dict[str, Any] | None = None
        self.notes: list[dict[str, Any]] = []
        self.dedupes: list[dict[str, Any]] = []
        self.timeline: dict[str, int | None] = {}
        self.fatal: str | None = None
        self._tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()

    # -- ledger --------------------------------------------------------------

    def find(self, call_id: str) -> Effect | None:
        return next((e for e in self.effects if e.call_id == call_id), None)

    def call_started(self, call_id: str, name: str, args: dict[str, Any]) -> Effect:
        e = Effect(call_id, name, dict(args), self.key_fn(name, args), self.ms(),
                   self.session_no)
        self.effects.append(e)
        self._log("ledger_call_started", call_id=call_id, name=name, args=args, key=e.key,
                  session=e.session)
        return e

    def call_responded(self, call_id: str) -> None:
        for e in self.effects:
            if call_id == e.call_id or call_id in e.duplicates:
                e.responded.add(call_id)

    def call_finished(self, call_id: str, state: str, result: dict[str, Any] | None = None,
                      detail: str = "") -> Effect | None:
        e = self.find(call_id)
        if e is None:
            return None
        e.state, e.result, e.detail = state, result, detail
        e.finished_ms, e.finished_wall = self.ms(), time.time()
        self._log("ledger_call_finished", call_id=call_id, state=state, result=result,
                  orphaned=e.orphaned, mode=self.mode)
        self._spawn(self._after_finished(e))
        return e

    def on_call(self, call_id: str, name: str, args: dict[str, Any]) -> Effect | None:
        """A function call from the model. Returns the existing effect when this is the
        same request (business key, within the window, not failed); the caller must
        then not execute it. The duplicate is answered from the existing job, now if
        it is final, else when it finishes."""
        if not self.cfg.dedupe:
            return None
        key = self.key_fn(name, args)
        now = self.ms()
        for e in reversed(self.effects):
            if e.key != key or e.call_id == call_id:
                continue
            if now - e.started_ms > self.cfg.dedupe_window_s * 1000 or e.state == "failed":
                return None
            e.duplicates.append(call_id)
            rec = {"at_ms": now, "call_id": call_id, "args": args, "key": key,
                   "original_call_id": e.call_id, "original_session": e.session,
                   "session": self.session_no, "state": e.state}
            self.dedupes.append(rec)
            self._log("dedupe_call", **rec)
            if e.state in FINAL_STATES:
                self._spawn(self._respond(e, call_id))
            return e
        return None

    # -- step 2 and 3 --------------------------------------------------------

    async def recover(self, handle: str | None,
                      connect: Callable[[str | None], Awaitable[Any]],
                      close_old: Callable[[], Awaitable[dict[str, Any]]] | None = None) -> str:
        """Called when the loss is detected. `connect(handle)` opens a connection (a
        resume with a handle, a new session with None) and returns the SDK session, or
        raises. `close_old()` tries to close the old connection and reports how it
        went. Returns the mode: "resumed", "new_session", "failed" or "fatal"."""
        t0 = self.ms()
        self.recovering = True
        self.session = None
        self.timeline["detected_ms"] = t0
        self.transcript.mark_loss()
        if self.cfg.close_old and close_old is not None:
            self._spawn(self._close_old(close_old))
        session = await self._resume_window(handle, connect, t0)
        if self.fatal:
            return self._done("fatal")
        if session is not None:
            self.session = session
            return self._done("resumed")
        session = await self._new_session(connect)
        if session is None:
            return self._done("fatal" if self.fatal else "failed")
        self.session = session
        self.session_no += 1
        for e in self.effects:   # ids of the lost session that were never answered
            if e.session < self.session_no and e.call_id not in e.responded:
                e.orphaned = True
        await self._restore(session)
        return self._done("new_session")

    def attach(self, session: Any) -> None:
        """The session the caller is using (before any loss), for duplicate answers."""
        self.session = session

    def _done(self, mode: str) -> str:
        self.mode = mode
        self.recovering = False
        self.timeline["recovered_ms"] = self.ms()
        self._log("recovery_done", mode=mode, timeline=self.timeline,
                  attempts=len(self.attempts), fatal=self.fatal)
        self.recovered.set()
        if mode in ("resumed", "new_session"):   # jobs that finished during the recovery
            for e in self.effects:
                if e.state in FINAL_STATES:
                    self._spawn(self._after_finished(e))
        return mode

    async def _resume_window(self, handle: str | None,
                             connect: Callable[[str | None], Awaitable[Any]],
                             t0: int) -> Any:
        cfg = self.cfg
        deadline = t0 + cfg.resume_window_s * 1000
        if handle is None:
            self._log("resume_window_skipped", reason="no resumable handle")
            return None
        self._log("resume_window_start", window_s=cfg.resume_window_s,
                  every_s=cfg.resume_every_s)
        k = 0
        while True:
            target = t0 + k * cfg.resume_every_s * 1000
            if target >= deadline:
                break
            await asyncio.sleep(max(0.0, (target - self.ms()) / 1000))
            k += 1
            start = self.ms()
            if start >= deadline:
                break
            timeout = min(cfg.attempt_timeout_s, (deadline - start) / 1000)
            try:
                session = await asyncio.wait_for(connect(handle), timeout)
            except Exception as exc:
                hung = isinstance(exc, TimeoutError)
                text = (f"no setupComplete within {timeout:.2f} s (window end)" if hung
                        else f"{type(exc).__name__}: {exc}")[:300]
                rec = {"attempt": k, "start_ms": start, "since_detect_ms": start - t0,
                       "ms_to_error": self.ms() - start, "hung": hung, "error": text}
                self.attempts.append(rec)
                self._log("resume_attempt_failed", **rec)
                if self.is_fatal(text):
                    self.fatal = text
                    return None
                continue
            rec = {"attempt": k, "start_ms": start, "since_detect_ms": start - t0,
                   "ms_to_setup": self.ms() - start, "accepted": True}
            self.attempts.append(rec)
            self._log("resume_accepted", **rec)
            self.timeline["setup_ms"] = self.ms()
            return session
        self.timeline["window_end_ms"] = self.ms()
        self._log("resume_window_end", attempts=len(self.attempts),
                  since_detect_ms=self.ms() - t0)
        return None

    async def _new_session(self, connect: Callable[[str | None], Awaitable[Any]]) -> Any:
        for i in range(1, self.cfg.new_session_attempts + 1):
            start = self.ms()
            self.timeline.setdefault("new_session_start_ms", start)
            try:
                session = await asyncio.wait_for(connect(None), self.cfg.new_session_timeout_s)
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"[:300]
                rec = {"try": i, "start_ms": start, "ms_to_error": self.ms() - start,
                       "error": text}
                self.new_session_tries.append(rec)
                self._log("new_session_failed", **rec)
                if self.is_fatal(text):
                    self.fatal = text
                    return None
                continue
            rec = {"try": i, "start_ms": start, "ms_to_setup": self.ms() - start}
            self.new_session_tries.append(rec)
            self.timeline["setup_ms"] = self.ms()
            self._log("new_session_open", **rec)
            return session
        return None

    def _status_line(self, e: Effect) -> str | None:
        what = self.describe(e.args, e.result)
        if e.state == "committed":
            when = time.strftime("%H:%M:%S %Z", time.localtime(e.finished_wall or time.time()))
            return self.cfg.note_committed.format(what=what, time=when)
        if e.state == "pending":
            return self.cfg.note_pending.format(what=what)
        return self.cfg.note_cancelled.format(what=what, detail=e.detail or e.state)

    def build_restore(self) -> tuple[list[types.Content], dict[str, Any]]:
        """The turns sent to a new session: transcript turns, then one user turn with
        the system note (intro, one status line per effect to report, outro)."""
        cfg = self.cfg
        now = self.ms()
        contents: list[types.Content] = []
        summary: list[dict[str, str]] = []
        for t in (self.transcript.last(cfg.summary_turns) if cfg.summary else []):
            text = re.sub(r"\s+", " ", t.text).strip() + (cfg.cut_marker if t.cut else "")
            contents.append(types.Content(role=t.role, parts=[types.Part(text=text)]))
            summary.append({"role": t.role, "text": text})
        status: list[str] = []
        reported: list[Effect] = []
        if cfg.status_notes:
            for e in self.effects:
                recent = e.finished_ms is not None and now - e.finished_ms <= cfg.recent_s * 1000
                if e.state == "pending" or recent:
                    line = self._status_line(e)
                    if line:
                        status.append(line)
                        reported.append(e)
        intro = cfg.note_intro if summary else cfg.note_intro_no_summary
        note = " ".join([intro, *status, cfg.note_outro])
        contents.append(types.Content(role="user", parts=[types.Part(text=note)]))
        for e in reported:
            e.reported = e.state
        return contents, {"summary": summary, "status_lines": status, "note": note,
                          "reported": [e.call_id for e in reported]}

    async def _restore(self, session: Any) -> None:
        contents, info = self.build_restore()
        try:
            async with self._send_lock:
                await session.send_client_content(turns=contents,
                                                  turn_complete=self.cfg.restore_turn_complete)
        except Exception as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"[:300]
        info["sent_ms"] = self.ms()
        info["turn_complete"] = self.cfg.restore_turn_complete
        info["orphaned"] = [e.call_id for e in self.effects if e.orphaned]
        self.restore = info
        self.timeline["restore_sent_ms"] = info["sent_ms"]
        self._log("restore_sent", **info)

    async def _close_old(self, close_old: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        start = self.ms()
        self._log("close_old_start")
        try:
            info = await asyncio.wait_for(close_old(), self.cfg.close_old_timeout_s)
        except Exception as exc:
            info = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        self.close_old_info = {"start_ms": start, "done_ms": self.ms(), **(info or {})}
        self._log("close_old_done", **self.close_old_info)

    # -- after the restore ---------------------------------------------------

    async def _after_finished(self, e: Effect) -> None:
        """A job finished. Duplicates issued on the live session get the result. After a
        fallback, an orphaned job whose new state the model was not told gets a
        follow-up note, unless a duplicate already carried the result. The original id
        is the caller's to answer while its session lives (before a loss, or after a
        resume). During a recovery nothing is sent; recover() reconciles at its end."""
        if self.recovering:
            return
        for cid in [c for c in e.duplicates if c not in e.responded]:
            await self._respond(e, cid)
        carried = any(c in e.responded for c in e.duplicates)
        if (self.mode == "new_session" and e.orphaned and not carried and self.cfg.status_notes
                and e.state in FINAL_STATES and e.reported not in (None, e.state)):
            await self._update_note(e)

    async def _update_note(self, e: Effect) -> None:
        t = self.ms()
        while not self.model_idle() and self.ms() - t < self.cfg.update_note_idle_wait_s * 1000:
            await asyncio.sleep(0.05)
        what = self.describe(e.args, e.result)
        when = time.strftime("%H:%M:%S %Z", time.localtime(e.finished_wall or time.time()))
        text = self.cfg.note_update.format(what=what, time=when)
        turn = types.Content(role="user", parts=[types.Part(text=text)])
        err = None
        try:
            async with self._send_lock:
                await self.session.send_client_content(turns=turn, turn_complete=True)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:300]
        e.reported = e.state
        rec = {"at_ms": self.ms(), "call_id": e.call_id, "text": text, "error": err,
               "waited_idle_ms": self.ms() - t}
        self.notes.append(rec)
        self._log("update_note_sent", **rec)

    async def _respond(self, e: Effect, call_id: str) -> None:
        if call_id in e.responded or self.session is None or e.state not in FINAL_STATES:
            return   # a pending job's duplicates are answered when it finishes
        e.responded.add(call_id)
        if e.state == "committed":
            payload = dict(e.result or {})
        else:
            payload = {"status": e.state, "detail": e.detail or e.state}
        payload["duplicate_of"] = e.call_id
        payload["note"] = "Same request as an earlier call; it was not executed again."
        fr = types.FunctionResponse(id=call_id, name=e.name, response=payload)
        session = self.session
        err = None
        try:
            async with self._send_lock:
                await session.send_tool_response(function_responses=[fr])
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:300]
        self._log("dedupe_response_sent", call_id=call_id, original_call_id=e.call_id,
                  response=payload, error=err)

    # -- plumbing ------------------------------------------------------------

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._log("recovery_task_error", error=repr(task.exception())[:300])

    def _log(self, event: str, **fields: Any) -> None:
        if self._log_cb is not None:
            self._log_cb(event, **fields)
