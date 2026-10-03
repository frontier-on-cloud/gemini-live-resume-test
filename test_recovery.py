"""Offline check of recovery.py (no key, no network): `uv run test_recovery.py`.

A fake connect() refuses every resume like the server did in round 3 (1011 after a
short delay) and opens new sessions; a fake session records what the client sends.
"""

from __future__ import annotations

import asyncio
import time

from recovery import Recovery, RecoveryConfig
from resume_test import QUOTA_RE, business_key


class FakeSession:
    def __init__(self, n: int) -> None:
        self.n = n
        self.client_content: list[tuple[list, bool]] = []
        self.tool_responses: list = []

    async def send_client_content(self, *, turns, turn_complete=True):
        self.client_content.append((turns if isinstance(turns, list) else [turns], turn_complete))

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.extend(function_responses)


class Clock:
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)


def make(cfg: RecoveryConfig, accept_resume_at: int | None = None, fatal: bool = False):
    clock = Clock()
    log: list[tuple[str, dict]] = []
    rec = Recovery(cfg, clock_ms=clock.ms, log=lambda e, **kw: log.append((e, kw)),
                   key_fn=business_key, is_fatal=lambda t: bool(QUOTA_RE.search(t)))
    sessions: list[FakeSession] = []
    tries = {"resume": 0}

    async def connect(handle):
        await asyncio.sleep(0.05)
        if handle is not None:
            tries["resume"] += 1
            if accept_resume_at is not None and tries["resume"] >= accept_resume_at:
                s = FakeSession(len(sessions) + 1)
                sessions.append(s)
                return s
            await asyncio.sleep(0.1)
            if fatal:
                raise RuntimeError("1011 RESOURCE_EXHAUSTED: quota exceeded")
            raise RuntimeError("APIError: 1011 None. Internal error encountered.")
        s = FakeSession(len(sessions) + 1)
        sessions.append(s)
        return s

    async def close_old():
        await asyncio.sleep(0.2)
        return {"close_sent": 1000, "close_rcvd": None}
    return rec, connect, close_old, sessions, log


def feed_b2_like(rec: Recovery) -> None:
    t = rec.transcript
    t.add("user", "Put me the 3:00 p.m. slot tomorrow, please.", 1)
    t.close_turn()                     # toolCall + turnComplete, no model text
    t.add("user", "While you do that, what should I bring?", 2)
    t.add("model", "I am booking your 3 ", 3)   # cut by the loss (no turnComplete)


async def test_fallback_committed() -> None:
    cfg = RecoveryConfig(resume_window_s=1.0, resume_every_s=0.3)
    rec, connect, close_old, sessions, log = make(cfg)
    feed_b2_like(rec)
    rec.call_started("call_1", "book_slot", {"slot": "tomorrow 3pm"})
    rec.call_finished("call_1", "committed", {"status": "booked", "slot": "tomorrow 3pm",
                                              "confirmation_id": "BK-1001"})
    mode = await rec.recover("h", connect, close_old)
    assert mode == "new_session", mode
    assert len(rec.attempts) == 4, rec.attempts           # +0, +0.3, +0.6, +0.9 s
    assert all("1011" in a["error"] for a in rec.attempts[:-1]), rec.attempts
    assert rec.attempts[-1]["hung"], rec.attempts[-1]   # +0.9 s: cut at the window end
    assert rec.attempts[-1]["since_detect_ms"] < 1000
    turns, tc = sessions[0].client_content[0]
    assert tc is True
    roles = [c.role for c in turns]
    assert roles == ["user", "user", "model", "user"], roles
    assert turns[2].parts[0].text.endswith("[cut off here by the connection loss]")
    note = turns[-1].parts[0].text
    assert "booking for tomorrow 3pm (BK-1001) was confirmed at" in note, note
    assert rec.find("call_1").orphaned
    # the model re-issues the call with drifted arguments: deduplicated, answered at once
    dup = rec.on_call("call_2", "book_slot", {"slot": "tomorrow at 3 PM"})
    assert dup is not None and dup.call_id == "call_1"
    await asyncio.sleep(0.05)
    fr = sessions[0].tool_responses[0]
    assert fr.id == "call_2" and fr.response["duplicate_of"] == "call_1"
    assert fr.response["confirmation_id"] == "BK-1001"
    await asyncio.sleep(0.3)   # close attempt finishes in the background
    assert rec.close_old_info and rec.close_old_info["close_sent"] == 1000
    await rec.aclose()
    print("fallback, committed during the window: ok;", note)


async def test_fallback_pending_then_update() -> None:
    cfg = RecoveryConfig(resume_window_s=0.5, resume_every_s=0.3)
    rec, connect, close_old, sessions, log = make(cfg)
    feed_b2_like(rec)
    rec.call_started("call_1", "book_slot", {"slot": "tomorrow 3pm"})
    assert await rec.recover("h", connect, close_old) == "new_session"
    note = sessions[0].client_content[0][0][-1].parts[0].text
    assert "is still being processed" in note, note
    rec.call_finished("call_1", "committed", {"status": "booked", "confirmation_id": "BK-1001"})
    await asyncio.sleep(0.1)
    assert len(sessions[0].client_content) == 2
    upd = sessions[0].client_content[1][0][0].parts[0].text
    assert "is now confirmed" in upd and "BK-1001" in upd, upd
    await rec.aclose()
    print("fallback, pending at restore, update note after commit: ok;", upd)


async def test_pending_duplicate_answered_on_finish() -> None:
    cfg = RecoveryConfig(resume_window_s=0.3, resume_every_s=0.3)
    rec, connect, close_old, sessions, log = make(cfg)
    rec.call_started("call_1", "book_slot", {"slot": "tomorrow 3pm"})
    await rec.recover("h", connect, close_old)
    assert rec.on_call("call_2", "book_slot", {"slot": "Tomorrow, 3pm"}) is not None
    await asyncio.sleep(0.05)
    assert sessions[0].tool_responses == []
    rec.call_finished("call_1", "committed", {"status": "booked", "confirmation_id": "BK-1001"})
    await asyncio.sleep(0.1)
    assert [f.id for f in sessions[0].tool_responses] == ["call_2"]
    assert len(sessions[0].client_content) == 1   # the duplicate carried it: no update note
    await rec.aclose()
    print("pending duplicate answered when the job commits, no extra note: ok")


async def test_ablation_no_status() -> None:
    cfg = RecoveryConfig(resume_window_s=0.3, resume_every_s=0.3, status_notes=False)
    rec, connect, close_old, sessions, log = make(cfg)
    feed_b2_like(rec)
    rec.call_started("call_1", "book_slot", {"slot": "tomorrow 3pm"})
    rec.call_finished("call_1", "committed", {"status": "booked", "confirmation_id": "BK-1001"})
    await rec.recover("h", connect, close_old)
    note = sessions[0].client_content[0][0][-1].parts[0].text
    assert "BK-1001" not in note and "booking" not in note, note
    await rec.aclose()
    print("ablation, summary only:", note)


async def test_resume_accepted_and_fatal() -> None:
    cfg = RecoveryConfig(resume_window_s=1.0, resume_every_s=0.3)
    rec, connect, close_old, sessions, log = make(cfg, accept_resume_at=2)
    rec.call_started("call_1", "book_slot", {"slot": "tomorrow 3pm"})
    assert await rec.recover("h", connect, close_old) == "resumed"
    assert sessions[0].client_content == [] and not rec.find("call_1").orphaned
    await rec.aclose()
    rec, connect, close_old, sessions, log = make(cfg, fatal=True)
    assert await rec.recover("h", connect, close_old) == "fatal"
    assert sessions == [] and "quota" in rec.fatal
    await rec.aclose()
    print("resume accepted in the window: ok; quota error stops the recovery: ok")


async def main() -> None:
    await test_fallback_committed()
    await test_fallback_pending_then_update()
    await test_pending_duplicate_answered_on_finish()
    await test_ablation_no_status()
    await test_resume_accepted_and_fatal()
    print("all recovery checks passed")


if __name__ == "__main__":
    asyncio.run(main())
