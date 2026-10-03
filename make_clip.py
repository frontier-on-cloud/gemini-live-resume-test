#!/usr/bin/env python3
"""Render the demo clip: the same silent connection loss with a booking pending, first
without recovery (round 3, B1 run 1), then with the client-side recovery (stage 2, BR1).

  2 s title card   "Without recovery"
  real time        results/B1.jsonl run 1, from the session start to 1.5 s after the
                   second refused resume
  2.5 s card       "Time skipped": the rest of the 15 minutes is not shown
  4 s still        the same run at its end, 15 minutes after the loss
  2 s title card   "With recovery"
  real time        results/clip/BR1_audio.jsonl run 1 (one BR1 session recorded with
                   --save-audio for this clip on 2026-10-03; not one of the four BR1 runs
                   in FINDINGS.md), from 3.6 s (just before the tool call) to 1 s after
                   the model's last audio

Every time and text shown is read from those files (ms since session start); nothing is
sped up inside a real-time part. The second half's model audio is
results/clip/audio_out/BR1_audio_run1_model.wav, placed as a Live client plays it: each
chunk at its arrival or right after the previous one, and whatever is still queued when
`interrupted` arrives is dropped. The user clips are assets/audio/book.wav and
did_you_book.wav at their send times. One gain (-1 dBFS peak) for the whole track. The
first half has no model audio: the model said nothing after its tool call.

The drawing code (fonts, colors, panel layout) is the one of
../gemini-live-stop-test/make_clip.py, copied here so this script runs on its own.

Output:
  results/clip/lockout-recovery.mp4  1280x720, 30 fps, H.264 + AAC (mono, 24 kHz), faststart
  results/clip/lockout-recovery.gif  800 px wide, 10 fps, no audio

Run (Homebrew ffmpeg is not needed; imageio-ffmpeg ships a static ffmpeg):
  uv run --with imageio-ffmpeg --with pillow --with numpy python make_clip.py
  ... make_clip.py --preview DIR     # PNG stills only
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
LOCK_JSONL, LOCK_RUN = RESULTS / "B1.jsonl", 1
REC_JSONL, REC_RUN = RESULTS / "clip" / "BR1_audio.jsonl", 1
REC_SIDECAR = RESULTS / "clip" / "audio_out" / "BR1_audio_run1_audio.json"
REC_WAV = RESULTS / "clip" / "audio_out" / "BR1_audio_run1_model.wav"
CLIPS = ROOT / "assets" / "audio"
USER_TEXT = {"book_request": "Book me the 3pm slot tomorrow, please.", "ask": "Did you book it?"}
OUT_DIR = RESULTS / "clip"
OUT_MP4 = OUT_DIR / "lockout-recovery.mp4"
OUT_GIF = OUT_DIR / "lockout-recovery.gif"

TITLE = "Gemini 3.8 Live: the connection drops during a booking"
W, H, FPS, SS, SR = 1280, 720, 30, 2, 24000
SPF = SR // FPS
CARD_S, SKIP_S, STILL_S = 2.0, 3.0, 4.0
LOCK_TAIL_MS = 1500     # after the second refusal
REC_FROM_MS = 0         # second half: the whole session, like the first
REC_TAIL_MS = 1000
HOLD_S = 1.5
GIF_FPS, GIF_W = 10, 800
PEAK = 0.89             # -1 dBFS
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# colors: the stop-test clip's (light surface, blue for server and model events, green
# for the committed booking) plus orange for the loss and the refused resumes
BG, PANEL, BORDER = (246, 245, 242), (255, 255, 255), (220, 218, 212)
INK, INK_2, INK_3 = (26, 26, 25), (82, 81, 78), (130, 129, 124)
TRACK, USER_FILL, NOTE_FILL = (232, 230, 225), (236, 234, 230), (244, 243, 240)
BLUE, BLUE_TINT = (42, 120, 214), (226, 237, 251)
GREEN, WHITE = (0, 131, 0), (255, 255, 255)
ORANGE = (235, 104, 52)


# ---------------------------------------------------------------- data
def load_events(path: Path, run: int) -> list[dict]:
    events = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    events = [e for e in events if e.get("run") == run]
    if not events:
        raise SystemExit(f"run {run} not found in {path}")
    return events


def finder(events: list[dict], where: str):
    def all_of(name: str, **match) -> list[dict]:
        return [e for e in events if e["event"] == name and e["t_ms"] >= 0
                and all(e.get(k) == v for k, v in match.items())]

    def one(name: str, **match) -> dict:
        hits = all_of(name, **match)
        if not hits:
            raise SystemExit(f"no {name} {match} in {where}")
        return hits[0]
    return all_of, one


def load_lockout() -> dict:
    ev = load_events(LOCK_JSONL, LOCK_RUN)
    all_of, one = finder(ev, f"{LOCK_JSONL.name} run {LOCK_RUN}")
    bh = one("blackhole_start")
    fails = all_of("reconnect_attempt_failed")
    assert fails and all((f.get("close") or {}).get("code") == 1011 for f in fails), "not all 1011"
    assert not all_of("resumed"), "a resume was accepted"
    lock = one("lockout")
    s2c = [e for e in all_of("old_flow_packet") if e["dir"] == "s2c"]
    commits = all_of("service_committed")
    assert len(commits) == 1, "expected one commit"
    assert not all_of("model_transcript"), "the model spoke; this clip says it did not"
    call = one("tool_call_received")
    return {
        "book_ms": one("user_audio_start", label="book_request")["t_ms"],
        "book_end_ms": one("user_audio_end", label="book_request")["t_ms"],
        "tool_call_ms": call["t_ms"], "call_id": call["call_id"],
        "job_ms": one("service_job_started")["t_ms"],
        "latency_ms": int(round(one("service_job_started")["latency_s"] * 1000)),
        "bh_ms": bh["t_ms"],
        "detect_ms": one("link_lost_detected")["t_ms"],
        "attempts": [{"n": f["attempt"], "start": f["start_ms"], "end": f["close"]["at_ms"],
                      "ms": f["ms_to_error"], "code": f["close"]["code"],
                      "reason": f["close"]["reason"]} for f in fails],
        "committed_ms": commits[0]["t_ms"], "confirmation": commits[0]["confirmation_id"],
        "slot": commits[0]["slot"],
        "end_ms": lock["t_ms"], "end_since_bh_ms": lock["since_blackhole_ms"],
        "first_server_ms": s2c[0]["t_ms"] if s2c else None,
        "last_server_ms": s2c[-1]["t_ms"] if s2c else None,
    }


def load_recovery() -> dict:
    ev = load_events(REC_JSONL, REC_RUN)
    all_of, one = finder(ev, f"{REC_JSONL.name} run {REC_RUN}")
    side = json.loads(REC_SIDECAR.read_text(encoding="utf-8"))
    restore = one("restore_sent")
    ask = one("user_audio_start", label="ask")
    texts = all_of("model_transcript")
    reply = "".join(e["text"] for e in texts if restore["t_ms"] <= e["t_ms"] < ask["t_ms"]).strip()
    answer = "".join(e["text"] for e in texts if e["t_ms"] >= ask["t_ms"]).strip()
    commits = all_of("service_committed")
    fails = all_of("resume_attempt_failed")
    new_conn = one("new_session_open")
    setup = next(e for e in all_of("first_server_message") if e["conn"] != 1)
    call = one("tool_call_received")
    assert len(commits) == 1, "expected one commit"
    assert len(all_of("tool_call_received")) == 1, "a call was re-issued; the clip says it was not"
    assert one("recovery_done")["mode"] == "new_session", "not a fallback run"
    assert all("1011" in f["error"] for f in fails), "a window attempt was not a 1011"
    clips = {c["label"]: c for c in side["user_clips"]}
    assert clips["ask"]["sent_start_ms"] == ask["t_ms"], "ask clip offset mismatch"
    return {
        "book_ms": one("user_audio_start", label="book_request")["t_ms"],
        "book_end_ms": one("user_audio_end", label="book_request")["t_ms"],
        "tool_call_ms": call["t_ms"], "call_id": call["call_id"],
        "job_ms": one("service_job_started")["t_ms"],
        "latency_ms": int(round(one("service_job_started")["latency_s"] * 1000)),
        "bh_ms": one("blackhole_start")["t_ms"],
        "detect_ms": one("link_lost_detected")["t_ms"],
        "attempts": [{"n": f["attempt"], "start": f["start_ms"], "end": f["t_ms"],
                      "ms": f["ms_to_error"], "code": 1011,
                      "reason": "Internal error encountered."} for f in fails],
        "window_end_ms": one("resume_window_end")["t_ms"],
        "new_start_ms": new_conn["start_ms"], "setup_ms": setup["t_ms"],
        "restore_ms": restore["t_ms"], "note": restore["note"],
        "status_line": restore["status_lines"][0],
        "committed_ms": commits[0]["t_ms"], "confirmation": commits[0]["confirmation_id"],
        "slot": commits[0]["slot"],
        "reply_ms": next(e["t_ms"] for e in texts if e["t_ms"] >= restore["t_ms"]),
        "reply": reply, "ask_ms": ask["t_ms"], "ask_end_ms": one("user_audio_end", label="ask")["t_ms"],
        "answer_ms": next(e["t_ms"] for e in texts if e["t_ms"] >= ask["t_ms"]), "answer": answer,
        "interrupted_ms": sorted(side["events"]["interrupted_ms"]),
        "side": side,
    }


# ---------------------------------------------------------------- audio
def decode(path: Path) -> np.ndarray:
    cmd = [FFMPEG, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
           "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<f4").astype(np.float32)


def model_playback(side: dict, wav: np.ndarray) -> list[tuple[float, np.ndarray]]:
    """(start_ms, samples) per chunk as a Live client plays them: a chunk starts at its
    arrival or when the previous one ends; `interrupted` drops what is still queued."""
    assert side["model_audio"]["sample_rate"] == SR
    interrupts = sorted(side["events"]["interrupted_ms"])
    out, cursor = [], 0.0
    for c in side["chunks"]:
        i0 = int(round(c["wav_offset_ms"] * SR / 1000))
        seg = wav[i0: i0 + int(round(c["duration_ms"] * SR / 1000))]
        start = max(float(c["t_ms"]), cursor)
        cut = next((t for t in interrupts if c["t_ms"] <= t < start + len(seg) * 1000 / SR), None)
        if cut is not None:
            seg = seg[: max(0, int((cut - start) * SR / 1000))]
        if len(seg):
            out.append((start, seg))
        cursor = start + len(seg) * 1000 / SR
    return out


def place(mix: np.ndarray, x: np.ndarray, at_ms: float) -> None:
    i = int(round(at_ms * SR / 1000))
    if i < 0:
        x, i = x[-i:], 0
    seg = x[: max(0, len(mix) - i)]
    mix[i: i + len(seg)] += seg


# ---------------------------------------------------------------- drawing primitives
FONT_CANDIDATES = {
    "regular": [("/System/Library/Fonts/HelveticaNeue.ttc", 0), ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
                ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0)],
    "bold": [("/System/Library/Fonts/HelveticaNeue.ttc", 1), ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
             ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0)],
    "mono": [("/System/Library/Fonts/Menlo.ttc", 0), ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0)],
    "mono_bold": [("/System/Library/Fonts/Menlo.ttc", 1), ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0)],
}
_fonts: dict = {}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key not in _fonts:
        for path, index in FONT_CANDIDATES[kind]:
            if Path(path).exists():
                _fonts[key] = ImageFont.truetype(path, size * SS, index=index)
                break
        else:
            raise SystemExit(f"no {kind} font found")
    return _fonts[key]


def tw(s: str, f) -> float:
    return f.getlength(s) / SS


def wrap(s: str, f, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if cur and tw(trial, f) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + [cur] if cur else lines


def fit(s: str, kind: str, size: int, max_w: float, min_size: int = 14):
    while size > min_size and tw(s, font(kind, size)) > max_w:
        size -= 1
    return font(kind, size)


def secs(ms: float) -> str:
    """ms as seconds with two decimals, rounded half up (7175 ms -> 7.18 s)."""
    return f"{math.floor(ms / 10 + 0.5) / 100:.2f} s"


def duration(ms: float) -> str:
    s = ms / 1000
    return f"{s:.2f} s" if s < 60 else f"{int(s // 60)} min {int(round(s % 60)):02d} s"


class Canvas:
    """Supersampled drawing surface; coordinates are always frame pixels. A canvas can
    cover only a region (origin ox, oy) and be pasted into a frame later."""

    def __init__(self, w: int = W, h: int = H, bg=BG, ox: float = 0, oy: float = 0) -> None:
        self.im = Image.new("RGB", (int(w * SS), int(h * SS)), bg)
        self.d = ImageDraw.Draw(self.im)
        self.ox, self.oy = ox, oy

    def _s(self, v):
        return [round((x - (self.ox if i % 2 == 0 else self.oy)) * SS) for i, x in enumerate(v)]

    def rrect(self, box, r, fill=None, outline=None, width=1):
        self.d.rounded_rectangle(self._s(box), radius=r * SS, fill=fill, outline=outline,
                                 width=round(width * SS) if outline else 0)

    def line(self, pts, fill, width=1):
        self.d.line(self._s([c for p in pts for c in p]), fill=fill, width=round(width * SS))

    def dot(self, x, y, r, fill, ring=None):
        if ring:
            self.d.ellipse(self._s((x - r - 2, y - r - 2, x + r + 2, y + r + 2)), fill=ring)
        self.d.ellipse(self._s((x - r, y - r, x + r, y + r)), fill=fill)

    def ring(self, x, y, r, color, width=2):
        self.d.ellipse(self._s((x - r, y - r, x + r, y + r)), outline=color, width=round(width * SS))

    def poly(self, pts, fill):
        self.d.polygon(self._s([c for p in pts for c in p]), fill=fill)

    def text(self, x, y, s, f, fill, anchor="la"):
        self.d.text(((x - self.ox) * SS, (y - self.oy) * SS), s, font=f, fill=fill, anchor=anchor)

    def paste(self, other: "Canvas") -> None:
        self.im.paste(other.im, (round((other.ox - self.ox) * SS), round((other.oy - self.oy) * SS)))

    def frame(self) -> Image.Image:
        return self.im.resize((W, H), Image.LANCZOS)


# ---------------------------------------------------------------- layout
M = 24
LEFT = (M, 86, 640, 500)
SERVICE = (656, 86, W - M, 282)
CONN = (656, 294, W - M, 500)
STRIP = (M, 512, W - M, H - 10)
AX_X0, AX_X1, AX_Y = 64, 1216, 666
ROW_Y = {1: 548, 2: 571, 3: 594, 4: 617}   # label rows (text top), 4 nearest the axis
F_LAB = ("regular", 19)
CONV_TOP, CONV_BOT = LEFT[1] + 54, LEFT[3] - 10


def draw_header(c: Canvas, t_ms: float, subtitle: str) -> None:
    c.text(M + 4, 16, TITLE, font("bold", 30), INK)
    c.text(M + 4, 52, subtitle, font("regular", 18), INK_2)
    clock = f"{t_ms / 1000:.2f} s"
    fc = font("mono_bold", 34)
    c.text(W - M - 4, 14, clock, fc, INK, anchor="ra")
    c.text(W - M - 4 - tw(clock, fc) - 10, 26, "t =", font("regular", 22), INK_2, anchor="ra")


def draw_panel(c: Canvas, box, title: str, note: str = "", note_fill=INK_2) -> None:
    c.rrect(box, 12, fill=PANEL, outline=BORDER, width=1)
    c.text(box[0] + 20, box[1] + 16, title, font("bold", 24), INK)
    if note:
        room = box[2] - box[0] - 60 - tw(title, font("bold", 24))
        c.text(box[2] - 20, box[1] + 20, note, fit(note, "regular", 18, room), note_fill, anchor="ra")


# -- conversation: messages scroll up when they no longer fit
F_BODY, F_ROLE, F_NOTE = ("regular", 24), ("regular", 17), ("regular", 16)
BODY_LH, NOTE_LH, PAD_X, PAD_Y = 30, 20, 16, 10
MAX_TEXT = 500


def message_blocks(msgs: list[dict]) -> list[dict]:
    """Height of each message block (role line + bubble + footnote + gap)."""
    out = []
    for m in msgs:
        if m["kind"] == "note":
            lines = wrap(m["text"], font(*F_NOTE), LEFT[2] - LEFT[0] - 40 - 2 * 12)
            h = 22 + len(lines) * NOTE_LH + 16 + 12
        else:
            lines = wrap(m["text"], font(*F_BODY), MAX_TEXT)
            h = 24 + len(lines) * BODY_LH + 2 * PAD_Y + (22 if m.get("foot") else 0) + 12
        out.append(m | {"lines": lines, "h": h})
    return out


def draw_messages(c: Canvas, msgs: list[dict], t_ms: float) -> None:
    blocks = message_blocks(msgs)
    shown = [b for b in blocks if t_ms >= b["at"]]
    room = CONV_BOT - CONV_TOP

    def offset_at(t: float) -> float:
        vis = [b for b in blocks if t >= b["at"]]
        return max(0.0, sum(b["h"] for b in vis) - room)
    # ease the scroll over 400 ms after each new message
    off = offset_at(t_ms)
    if shown:
        last = shown[-1]["at"]
        before = offset_at(last - 1)
        k = min(1.0, (t_ms - last) / 400)
        k = k * k * (3 - 2 * k)
        off = before + (off - before) * k
    sub = Canvas(LEFT[2] - LEFT[0] - 4, room, PANEL, LEFT[0] + 2, CONV_TOP)
    x0, x1 = LEFT[0] + 20, LEFT[2] - 20
    y = CONV_TOP - off
    for b in shown:
        if b["kind"] == "note":
            sub.text(x0, y, b["role"], font(*F_ROLE), INK_2)
            bh = len(b["lines"]) * NOTE_LH + 16
            sub.rrect((x0, y + 22, x1, y + 22 + bh), 10, fill=NOTE_FILL, outline=BORDER, width=1)
            for i, s in enumerate(b["lines"]):
                sub.text(x0 + 12, y + 22 + 8 + i * NOTE_LH, s, font(*F_NOTE), INK_2)
        else:
            f = font(*F_BODY)
            bw = max(tw(s, f) for s in b["lines"]) + 2 * PAD_X
            bh = len(b["lines"]) * BODY_LH + 2 * PAD_Y
            if b["kind"] == "user":
                bx1 = x1
                bx0 = bx1 - bw
                sub.text(bx1, y, b["role"], font(*F_ROLE), INK_2, anchor="ra")
                sub.rrect((bx0, y + 24, bx1, y + 24 + bh), 16, fill=USER_FILL)
            else:
                bx0, bx1 = x0, x0 + bw
                sub.dot(bx0 + 6, y + 10, 5, BLUE)
                sub.text(bx0 + 18, y, b["role"], font(*F_ROLE), INK_2)
                sub.rrect((bx0, y + 24, bx1, y + 24 + bh), 16, fill=BLUE_TINT, outline=BLUE, width=2)
            for i, s in enumerate(b["lines"]):
                sub.text(bx0 + PAD_X, y + 24 + PAD_Y + i * BODY_LH - 1, s, f, INK)
            if b.get("foot") and t_ms >= b.get("foot_at", 0):
                sub.text(bx0 + 4, y + 24 + bh + 4, b["foot"], font(*F_NOTE), INK_3)
        y += b["h"]
    c.paste(sub)


def silence_note(t_ms: float, from_ms: float, until_ms: float | None) -> tuple[str, tuple]:
    if t_ms < from_ms:
        return "", INK_2
    if until_ms is not None and t_ms >= until_ms:
        return f"silence after the request: {duration(until_ms - from_ms)}, then the model spoke", INK_2
    return f"silence after the request: {duration(t_ms - from_ms)}", ORANGE


# -- booking service
def draw_service(c: Canvas, d: dict, t_ms: float, reported: str) -> None:
    draw_panel(c, SERVICE, "Booking service", f"fake service, commits {d['latency_ms'] / 1000:.1f} s after the call")
    x0, y0, x1, _ = SERVICE
    ix0, ix1 = x0 + 20, x1 - 20
    if t_ms < d["job_ms"]:
        c.text(ix0, y0 + 64, "idle, no tool call yet", font("regular", 22), INK_3)
        return
    y = y0 + 54
    c.text(ix0, y, "job started", font("bold", 22), INK)
    cid_x = ix0 + tw("job started", font("bold", 22)) + 12
    c.text(cid_x, y + 4, d["call_id"], font("mono", 17), INK_2)
    c.text(ix1, y + 3, secs(d["job_ms"]), font("regular", 18), INK_2, anchor="ra")
    by = y + 36
    frac = min(1.0, max(0.0, (t_ms - d["job_ms"]) / d["latency_ms"]))
    done = t_ms >= d["committed_ms"]
    c.rrect((ix0, by, ix1, by + 16), 8, fill=TRACK)
    if frac > 0:
        c.rrect((ix0, by, ix0 + max(16, (ix1 - ix0) * frac), by + 16), 8, fill=GREEN if done else INK_2)
    s = (f"committed at {secs(d['committed_ms'])}" if done else
         f"elapsed {(t_ms - d['job_ms']) / 1000:.2f} s of {d['latency_ms'] / 1000:.1f} s")
    c.text(ix0, by + 22, s, font("regular", 18), INK_2)
    if done:
        cy = by + 50
        c.rrect((ix0, cy, ix1, cy + 44), 10, fill=GREEN)
        c.line([(ix0 + 16, cy + 23), (ix0 + 24, cy + 31), (ix0 + 38, cy + 14)], WHITE, width=4)
        label = f"COMMITTED, {d['confirmation']}"
        c.text(ix0 + 52, cy + 22, label, font("bold", 24), WHITE, anchor="lm")
        room = ix1 - 14 - (ix0 + 52 + tw(label, font("bold", 24)) + 16)
        c.text(ix1 - 14, cy + 22, reported, fit(reported, "regular", 18, room), WHITE, anchor="rm")


# -- connection
def draw_connection(c: Canvas, d: dict, t_ms: float, status: tuple[str, tuple],
                    rows: list[tuple[str, tuple]]) -> None:
    draw_panel(c, CONN, "Connection", "client pings every 0.5 s")
    x0, y0, x1, _ = CONN
    ix0, ix1 = x0 + 20, x1 - 20
    text, color = status
    c.dot(ix0 + 7, y0 + 66, 7, color)
    c.text(ix0 + 24, y0 + 66, text, fit(text, "bold", 21, ix1 - ix0 - 24), INK, anchor="lm")
    for i, (s, col) in enumerate(rows[:4]):
        c.text(ix0, y0 + 92 + i * 26, s, fit(s, "regular", 18, ix1 - ix0), col)


def attempt_rows(d: dict, t_ms: float, bh_ms: float) -> list[tuple[str, tuple]]:
    rows = []
    for a in d["attempts"]:
        if t_ms < a["start"]:
            break
        head = f"resume #{a['n']} at {secs(a['start'])}"
        if t_ms < a["end"]:
            rows.append((f"{head}: waiting for setupComplete", INK_3))
        else:
            rows.append((f"{head}: closed {a['code']} “{a['reason']}” after {a['ms']} ms", INK))
    return rows


# -- events strip
def strip_layout(ticks: list[tuple], t0: float, t1: float):
    f_lab = font(*F_LAB)
    boxes, leaders = [], []
    for label, at, _, row, side, _ in ticks:
        x, ly = tx(at, t0, t1), ROW_Y[row]
        w = tw(label, f_lab)
        boxes.append((label, (x + 8, ly, x + 8 + w, ly + 21) if side == "right" else (x - 8 - w, ly, x - 8, ly + 21)))
        leaders.append((label, (x - 1, ly + 2, x + 1, AX_Y)))
    return boxes, leaders


def overlaps(ticks: list[tuple], t0: float, t1: float, extra: list[tuple] = ()) -> list[str]:
    def hit(a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
    boxes, leaders = strip_layout(ticks, t0, t1)
    boxes += list(extra)
    fe = font("bold", 22)
    head = "Events" + "  seconds since session start"
    boxes.append(("header", (STRIP[0] + 20, STRIP[1] + 12, STRIP[0] + 30 + tw(head, fe), STRIP[1] + 36)))
    bad = [f"{a!r} x {b!r}" for i, (a, ba) in enumerate(boxes) for b, bb in boxes[i + 1:] if hit(ba, bb)]
    bad += [f"{a!r} x leader of {b!r}" for a, ba in boxes for b, lb in leaders if a != b and hit(ba, lb)]
    bad += [f"{a!r} outside the strip" for a, ba in boxes if ba[0] < STRIP[0] + 8 or ba[2] > STRIP[2] - 8]
    play = (AX_X0 - 7, AX_Y - 22, AX_X1 + 7, AX_Y - 10)
    bad += [f"{a!r} x playhead path" for a, ba in boxes if hit(ba, play)]
    return bad


def tx(t_ms: float, t0: float, t1: float) -> float:
    return AX_X0 + (AX_X1 - AX_X0) * (t_ms - t0) / (t1 - t0)


def draw_strip(c: Canvas, ticks: list[tuple], t_ms: float, t0: float, t1: float,
               every_s: int = 1, label_every: int = 2, playhead: bool = True,
               axis_note: str = "seconds since session start", marks: list[tuple] = ()) -> None:
    c.rrect(STRIP, 12, fill=PANEL, outline=BORDER, width=1)
    c.text(STRIP[0] + 20, STRIP[1] + 12, "Events", font("bold", 22), INK)
    c.text(STRIP[0] + 20 + tw("Events", font("bold", 22)) + 10, STRIP[1] + 16, axis_note,
           font("regular", 18), INK_2)
    now_x = tx(min(max(t_ms, t0), t1), t0, t1)
    c.line([(AX_X0, AX_Y), (AX_X1, AX_Y)], TRACK, width=3)
    c.line([(AX_X0, AX_Y), (now_x, AX_Y)], INK_3, width=3)
    first = math.ceil(t0 / 1000 / every_s) * every_s
    s = first
    while s * 1000 <= t1:
        x = tx(s * 1000, t0, t1)
        c.line([(x, AX_Y + 4), (x, AX_Y + 9)], INK_3, width=1)
        if (s // every_s) % label_every == 0:
            c.text(x, AX_Y + 12, f"{s} s", font("regular", 17), INK_3, anchor="ma")
        s += every_s
    for at, color in marks:   # small unlabeled marks on the axis (the +15 min still)
        if t_ms >= at:
            x = tx(at, t0, t1)
            c.line([(x, AX_Y - 9), (x, AX_Y + 9)], color, width=2)
    shown = [tk for tk in ticks if t_ms >= tk[1]]
    for label, at, color, row, side, _ in shown:
        x, ly = tx(at, t0, t1), ROW_Y[row]
        c.line([(x, AX_Y), (x, ly + 2)], color, width=2)
        if side == "right":
            c.text(x + 8, ly, label, font(*F_LAB), INK)
        else:
            c.text(x - 8, ly, label, font(*F_LAB), INK, anchor="ra")
    for label, at, color, row, side, marker in shown:
        x = tx(at, t0, t1)
        if marker == "ring":
            c.dot(x, AX_Y, 9, PANEL)
            c.ring(x, AX_Y, 9, color, width=2)
        else:
            c.dot(x, AX_Y, 6, color, ring=PANEL)
    if playhead:
        c.poly([(now_x - 7, AX_Y - 20), (now_x + 7, AX_Y - 20), (now_x, AX_Y - 10)], INK)


# ---------------------------------------------------------------- the two halves
def lock_ticks(d: dict) -> list[tuple]:
    a1, a2 = d["attempts"][0], d["attempts"][1]
    return [
        (f"toolCall {secs(d['tool_call_ms'])}", d["tool_call_ms"], BLUE, 4, "left", "dot"),
        (f"network lost {secs(d['bh_ms'])}", d["bh_ms"], ORANGE, 3, "left", "dot"),
        (f"loss detected {secs(d['detect_ms'])}", d["detect_ms"], INK, 2, "left", "dot"),
        (f"resume #1 refused (1011) {secs(a1['end'])}", a1["end"], ORANGE, 1, "right", "dot"),
        (f"committed {secs(d['committed_ms'])}", d["committed_ms"], GREEN, 2, "right", "dot"),
        (f"resume #2 refused (1011) {secs(a2['end'])}", a2["end"], ORANGE, 1, "left", "dot"),
    ]


def render_lock(d: dict, t_ms: float, end_ms: float) -> Image.Image:
    c = Canvas()
    draw_header(c, t_ms, "Without recovery. Measured run: B1 run 1 (real packet loss, model idle, "
                         "resume every 10 s). Real time, session clock.")
    note, col = silence_note(t_ms, d["book_end_ms"], None)
    draw_panel(c, LEFT, "Conversation", note, col)
    draw_messages(c, [{"kind": "user", "at": d["book_ms"], "role": f"User, {secs(d['book_ms'])}",
                       "text": USER_TEXT["book_request"]}], t_ms)
    draw_service(c, d, t_ms, "not reported")
    if t_ms < d["bh_ms"]:
        status = ("connected (connection 1, session resumption on)", BLUE)
    elif t_ms < d["detect_ms"]:
        status = ("packets dropped both ways, nothing closed", ORANGE)
    else:
        status = (f"lost: detected at {secs(d['detect_ms'])}, resuming with the handle", ORANGE)
    rows = attempt_rows(d, t_ms, d["bh_ms"])
    shown = [a for a in d["attempts"] if t_ms >= a["end"]]
    nxt = next((a for a in d["attempts"] if a["start"] > t_ms), None)
    if shown and nxt is not None and len(rows) < 4:
        rows.append((f"next attempt at {secs(nxt['start'])} (every 10 s)", INK_3))
    draw_connection(c, d, t_ms, status, rows)
    draw_strip(c, lock_ticks(d), t_ms, 0, end_ms)
    return c.frame()


def still_ticks(d: dict) -> list[tuple]:
    return [
        (f"network lost {secs(d['bh_ms'])}, committed {secs(d['committed_ms'])}",
         d["committed_ms"], GREEN, 4, "right", "dot"),
        (f"server's last TCP retransmission {d['last_server_ms'] / 1000:.1f} s",
         d["last_server_ms"], INK_2, 3, "left", "ring"),
        (f"resume #{d['attempts'][-1]['n']} refused (1011) {secs(d['attempts'][-1]['end'])}",
         d["attempts"][-1]["end"], ORANGE, 2, "left", "dot"),
    ]


def render_still(d: dict) -> Image.Image:
    t = d["end_ms"]
    c = Canvas()
    draw_header(c, t, f"Without recovery. B1 run 1 at its end, {d['end_since_bh_ms'] / 60000:.1f} min "
                      "after the loss. The time in between is not shown.")
    note, col = silence_note(t, d["book_end_ms"], None)
    draw_panel(c, LEFT, "Conversation", note, col)
    draw_messages(c, [{"kind": "user", "at": d["book_ms"], "role": f"User, {secs(d['book_ms'])}",
                       "text": USER_TEXT["book_request"]}], t)
    x0 = LEFT[0] + 20
    c.text(x0, 300, "+15 min: still refused,", font("bold", 34), INK)
    c.text(x0, 344, "the user was never told.", font("bold", 34), INK)
    c.text(x0, 400, "The model said nothing after its tool call; the booking was", font("regular", 18), INK_2)
    c.text(x0, 424, f"committed {(d['committed_ms'] - d['bh_ms']) / 1000:.1f} s after the loss.",
           font("regular", 18), INK_2)
    draw_service(c, d, t, "never reported")
    a = d["attempts"]
    lo = min(x["ms"] for x in a)
    hi = max(x["ms"] for x in a)
    rows = [(f"resume #1 to #{len(a)}, from {secs(a[0]['start'])} to {secs(a[-1]['start'])}", INK),
            (f"all {len(a)} closed {a[0]['code']} “{a[0]['reason']}”", INK),
            (f"each after {lo} to {hi} ms; none hung, none accepted", INK),
            ("the old socket is still open on the client", INK_2)]
    draw_connection(c, d, t, ("locked out: no resume accepted in 15 min", ORANGE), rows)
    draw_strip(c, still_ticks(d), t, 0, 900_000, every_s=60, label_every=2, playhead=False,
               axis_note="the whole run, seconds since session start",
               marks=[(x["end"], ORANGE) for x in a])
    return c.frame()


def rec_ticks(d: dict) -> list[tuple]:
    a = d["attempts"]
    return [
        (f"toolCall {secs(d['tool_call_ms'])}", d["tool_call_ms"], BLUE, 4, "left", "dot"),
        (f"network lost {secs(d['bh_ms'])}", d["bh_ms"], ORANGE, 3, "left", "dot"),
        (f"loss detected {secs(d['detect_ms'])}", d["detect_ms"], INK, 2, "left", "dot"),
        (f"{len(a)} resumes refused (1011)", a[0]["end"], ORANGE, 1, "right", "dot"),
        (f"committed {secs(d['committed_ms'])}", d["committed_ms"], GREEN, 2, "right", "dot"),
        (f"new session + status note {secs(d['restore_ms'])}", d["restore_ms"], BLUE, 3, "right", "ring"),
        (f"model speaks {secs(d['reply_ms'])}", d["reply_ms"], BLUE, 4, "right", "dot"),
    ]


def rec_messages(d: dict, cut_ms: float | None) -> list[dict]:
    note = d["note"]
    status = d["status_line"]
    assert status in note
    intro = note.split(".")[0]
    excerpt = f"{intro} […] {status} […]"
    foot = (f"playback stopped at {secs(cut_ms)}: the user spoke (interrupted)" if cut_ms else None)
    return [
        {"kind": "user", "at": d["book_ms"], "role": f"User, {secs(d['book_ms'])}",
         "text": USER_TEXT["book_request"]},
        {"kind": "note", "at": d["restore_ms"],
         "role": f"App to the new session (text), {secs(d['restore_ms'])}: last turn + note",
         "text": excerpt},
        {"kind": "model", "at": d["reply_ms"], "role": f"Model (spoken reply), {secs(d['reply_ms'])}",
         "text": d["reply"], "foot": foot, "foot_at": cut_ms or 0},
        {"kind": "user", "at": d["ask_ms"], "role": f"User, {secs(d['ask_ms'])}", "text": USER_TEXT["ask"]},
        {"kind": "model", "at": d["answer_ms"], "role": f"Model (spoken reply), {secs(d['answer_ms'])}",
         "text": d["answer"]},
    ]


def render_rec(d: dict, t_ms: float, t0: float, t1: float, cut_ms: float | None,
               first_audio_ms: float) -> Image.Image:
    c = Canvas()
    draw_header(c, t_ms, "With recovery. Measured run: BR1, one session recorded with audio for "
                         "this clip. Real time, session clock.")
    note, col = silence_note(t_ms, d["book_end_ms"], first_audio_ms)
    draw_panel(c, LEFT, "Conversation", note, col)
    draw_messages(c, rec_messages(d, cut_ms), t_ms)
    draw_service(c, d, t_ms, "reported in the status note" if t_ms >= d["restore_ms"] else "not reported")
    if t_ms < d["bh_ms"]:
        status = ("connected (connection 1, session resumption on)", BLUE)
    elif t_ms < d["detect_ms"]:
        status = ("packets dropped both ways, nothing closed", ORANGE)
    elif t_ms < d["window_end_ms"]:
        status = (f"lost: detected at {secs(d['detect_ms'])}, 4 s resume window", ORANGE)
    elif t_ms < d["setup_ms"]:
        status = ("window over: opening a new session, no handle", ORANGE)
    else:
        status = (f"new session up at {secs(d['setup_ms'])}; old call id left unanswered", BLUE)
    draw_connection(c, d, t_ms, status, attempt_rows(d, t_ms, d["bh_ms"]))
    draw_strip(c, rec_ticks(d), t_ms, t0, t1, marks=[(x["end"], ORANGE) for x in d["attempts"][1:]])
    return c.frame()


def render_card(lines: list[tuple[str, int, tuple]]) -> Image.Image:
    c = Canvas()
    total = sum(size + 18 for _, size, _ in lines) - 18
    y = H / 2 - total / 2
    for s, size, col in lines:
        c.text(W / 2, y + size / 2, s, font("bold" if size >= 40 else "regular", size), col, anchor="mm")
        y += size + 18
    return c.frame()


# ---------------------------------------------------------------- main
def voiced_rms_db(x: np.ndarray) -> float:
    fr = x[: len(x) // 480 * 480].reshape(-1, 480)
    r = np.sqrt((fr ** 2).mean(axis=1))
    v = r[r > 10 ** (-50 / 20)]
    return 20 * math.log10(float(np.sqrt((v ** 2).mean()))) if len(v) else float("-inf")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preview", type=Path, help="write PNG stills to this folder and stop")
    a = ap.parse_args()

    L = load_lockout()
    R = load_recovery()
    segments = model_playback(R["side"], decode(REC_WAV))
    cut = next((t for t in R["interrupted_ms"] if t > R["reply_ms"]), None)
    first_audio = min(s for s, _ in segments)
    model_end = max(s + len(x) * 1000 / SR for s, x in segments)
    lock_end = L["attempts"][1]["end"] + LOCK_TAIL_MS
    rec_t0, rec_t1 = REC_FROM_MS, model_end + REC_TAIL_MS
    skipped_ms = L["end_ms"] - lock_end
    more = sum(1 for x in L["attempts"] if x["start"] > lock_end)

    print(f"without recovery: book {L['book_ms']}, toolCall {L['tool_call_ms']}, loss {L['bh_ms']}, "
          f"detected {L['detect_ms']}, commit {L['committed_ms']}, attempts {len(L['attempts'])} "
          f"(shown in real time: {sum(1 for x in L['attempts'] if x['end'] <= lock_end)}), "
          f"real time to {lock_end} ms, skipped {skipped_ms} ms, end {L['end_ms']}")
    print(f"with recovery: loss {R['bh_ms']}, detected {R['detect_ms']}, window end {R['window_end_ms']}, "
          f"commit {R['committed_ms']}, setupComplete {R['setup_ms']}, restore {R['restore_ms']}, "
          f"model {R['reply_ms']} ({R['reply']!r}), ask {R['ask_ms']}, interrupted {R['interrupted_ms']}, "
          f"answer {R['answer_ms']} ({R['answer']!r}); model audio "
          + ", ".join(f"{s:.0f}-{s + len(x) * 1000 / SR:.0f}" for s, x in segments))

    bad = (overlaps(lock_ticks(L), 0, lock_end) + overlaps(rec_ticks(R), rec_t0, rec_t1)
           + overlaps(still_ticks(L), 0, 900_000))
    for b in bad:
        print(f"OVERLAP in the events strip: {b}")
    if bad:
        raise SystemExit("fix the strip layout first")

    cards = {
        "card_without": render_card([("Without recovery", 46, INK)]),
        "skip": render_card([("Time skipped", 46, INK),
                             (f"{duration(skipped_ms)} of this run are not shown: "
                              f"{more} more resume attempts, one every 10 s.", 24, INK_2)]),
        "card_with": render_card([("With recovery", 46, INK)]),
    }
    if a.preview:
        a.preview.mkdir(parents=True, exist_ok=True)
        for k, im in cards.items():
            im.save(a.preview / f"card-{k}.png")
        for t in (L["bh_ms"] + 500, L["committed_ms"] + 300, 12_000, lock_end):
            render_lock(L, t, lock_end).save(a.preview / f"lock-{int(t):05d}.png")
        render_still(L).save(a.preview / "still.png")
        for t in (R["detect_ms"] + 1200, R["restore_ms"] + 300, R["reply_ms"] + 1500,
                  R["ask_ms"] + 600, R["answer_ms"] + 1500, rec_t1):
            render_rec(R, t, rec_t0, rec_t1, cut, first_audio).save(a.preview / f"rec-{int(t):05d}.png")
        print(f"stills in {a.preview}")
        return

    n_card, n_skip, n_still = (int(round(s * FPS)) for s in (CARD_S, SKIP_S, STILL_S))
    n_lock = math.ceil(lock_end / 1000 * FPS)
    n_rec = math.ceil((rec_t1 - rec_t0) / 1000 * FPS)
    n_hold = int(round(HOLD_S * FPS))
    parts = [("card_without", n_card), ("without", n_lock), ("skip", n_skip), ("still", n_still),
             ("card_with", n_card), ("with", n_rec + n_hold)]
    total = sum(n for _, n in parts)
    print("frames: " + " + ".join(f"{k} {n}" for k, n in parts) + f" = {total} ({total / FPS:.2f} s)")

    # audio: one track, one gain
    track = np.zeros(total * SPF, dtype=np.float32)
    off = n_card * SPF
    lock_mix = np.zeros(n_lock * SPF, dtype=np.float32)
    place(lock_mix, decode(CLIPS / "book.wav"), L["book_ms"])
    track[off: off + len(lock_mix)] += lock_mix
    off = (n_card + n_lock + n_skip + n_still + n_card) * SPF
    rec_mix = np.zeros((n_rec + n_hold) * SPF, dtype=np.float32)
    place(rec_mix, decode(CLIPS / "book.wav"), R["book_ms"] - rec_t0)
    place(rec_mix, decode(CLIPS / "did_you_book.wav"), R["ask_ms"] - rec_t0)
    for start, seg in segments:
        place(rec_mix, seg, start - rec_t0)
    track[off: off + len(rec_mix)] += rec_mix
    peak = float(np.abs(track).max())
    gain = PEAK / peak
    track *= gain
    print(f"audio: one gain {20 * math.log10(gain):+.2f} dB (raw peak {peak:.3f} -> -1 dBFS); "
          f"voiced rms {voiced_rms_db(track):.1f} dBFS")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        mix_path = Path(tmp) / "mix.wav"
        pcm = np.clip(np.round(track * 32767), -32768, 32767).astype("<i2")
        with wave.open(str(mix_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        cmd = [FFMPEG, "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
               "-i", str(mix_path), "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k", "-ar", str(SR), "-ac", "1",
               "-movflags", "+faststart", str(OUT_MP4)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for kind, n in parts:
            if kind in cards:
                buf = cards[kind].tobytes()
                for _ in range(n):
                    proc.stdin.write(buf)
            elif kind == "still":
                buf = render_still(L).tobytes()
                for _ in range(n):
                    proc.stdin.write(buf)
            elif kind == "without":
                for i in range(n):
                    proc.stdin.write(render_lock(L, min(i * 1000 / FPS, lock_end), lock_end).tobytes())
            elif kind == "with":
                for i in range(n):
                    t = min(rec_t0 + i * 1000 / FPS, rec_t1)
                    proc.stdin.write(render_rec(R, t, rec_t0, rec_t1, cut, first_audio).tobytes())
        proc.stdin.close()
        if proc.wait() != 0:
            raise SystemExit("ffmpeg (mp4) failed")

    vf = (f"fps={GIF_FPS},scale={GIF_W}:-1:flags=lanczos,split[a][b];"
          "[a]palettegen=max_colors=256:stats_mode=full:reserve_transparent=0[p];"
          "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(OUT_MP4), "-an", "-vf", vf, str(OUT_GIF)],
                   check=True)
    starts, acc = {}, 0
    for kind, n in parts:
        starts.setdefault(kind, []).append(round(acc / FPS, 2))
        acc += n
    print("part starts (s): " + ", ".join(f"{k} {v}" for k, v in starts.items()))
    for p in (OUT_MP4, OUT_GIF):
        print(f"wrote {p.relative_to(ROOT)} ({p.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
