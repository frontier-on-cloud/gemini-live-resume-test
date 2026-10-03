#!/usr/bin/env python3
"""Render the demo clip (v2): the same silent connection loss with a booking pending, first
without recovery, then with the client-side recovery. Both halves are sessions recorded for
the clip on 2026-10-03 with --save-audio and the Kokoro-82M af_heart user voice
(--voice-dir assets/audio/af_heart), in the round 3 container. They are not among the 43
sessions of the measured tables.

  2 s title card   "The connection drops during a booking"
  real time        results/clip/B1_60s_af_heart.jsonl run 1 (B1 timing, resume at
                   detection then every 10 s, for at most 60 s after the loss), from the
                   session start to 1.5 s after the later of the first refusal and the commit
  time compressed  the rest of that session, x4, to 1 s after the last refused resume; the
                   header says "time compressed x4" and the events axis has a break there
  5 s still        the end of that session, with the round 3 result: after 15 minutes, still
                   refused, the user is never told (B1 and B2, FINDINGS.md)
  2 s card         "With recovery"
  real time        results/clip/BR1_af_heart.jsonl run 1 (BR1 timing), from the session
                   start to 1 s after the model's last audio

Every time and text shown is read from those files (ms since session start). Captions are
verbatim: the user's from the server's input transcription, the model's from its output
transcription. Audio: the user clips named in each run's sidecar
(results/clip/audio_out/*_audio.json, `source`) at their send times, and the model's audio
(results/clip/audio_out/BR1_af_heart_run1_model.wav) placed as a Live client plays it: each
chunk at its arrival or right after the previous one, and whatever is still queued when
`interrupted` arrives is dropped. One gain (-1 dBFS peak) for the whole track. The first
half has no model audio: the model said nothing between its tool call and the loss. The
compressed part and the still are silent; nothing was sent or received there.

The drawing code (fonts, colors, panel layout) is the one of
../gemini-live-stop-test/make_clip.py, copied here so this script runs on its own. The
first clip (results/clip/lockout-recovery.mp4: B1 run 1 and results/clip/BR1_audio.jsonl,
macOS `say` voice) was rendered by this script as of commit a2bc678.

Output:
  results/clip/lockout-recovery-v2.mp4  1280x720, 30 fps, H.264 + AAC (mono, 24 kHz), faststart
  results/clip/lockout-recovery-v2.gif  800 px wide, 10 fps, no audio

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
CLIP_DIR = ROOT / "results" / "clip"
LOCK_JSONL, LOCK_RUN = CLIP_DIR / "B1_60s_af_heart.jsonl", 1
LOCK_SIDECAR = CLIP_DIR / "audio_out" / "B1_60s_af_heart_run1_audio.json"
REC_JSONL, REC_RUN = CLIP_DIR / "BR1_af_heart.jsonl", 1
REC_SIDECAR = CLIP_DIR / "audio_out" / "BR1_af_heart_run1_audio.json"
REC_WAV = CLIP_DIR / "audio_out" / "BR1_af_heart_run1_model.wav"
OUT_MP4 = CLIP_DIR / "lockout-recovery-v2.mp4"
OUT_GIF = CLIP_DIR / "lockout-recovery-v2.gif"

TITLE = "Gemini 3.8 Live: the connection drops during a booking"
VOICE = "Kokoro-82M af_heart"
W, H, FPS, SS, SR = 1280, 720, 30, 2, 24000
SPF = SR // FPS
CARD_S, END_S, HOLD_S = 2.0, 5.0, 1.5
FAST = 4                # time compression of the without-recovery excerpt after LOCK_A_TAIL
LOCK_A_TAIL_MS = 1500   # real time until this long after the first refusal and the commit
LOCK_B_TAIL_MS = 1000   # compressed part ends this long after the last refusal
REC_FROM_MS = 0         # second half: the whole session, like the first
REC_TAIL_MS = 1000
GIF_FPS, GIF_W = 10, 800
PEAK = 0.89             # -1 dBFS
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# round 3 (FINDINGS.md, README finding 3): the source of the end card's 15 minutes
ROUND3 = "round 3 (B1 and B2, 5 runs, 450 of 450 resumes refused, the last at +892 s)"

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


def heard(all_of, start_ms: float, end_ms: float) -> str:
    """The server's input transcription of one user clip, verbatim."""
    text = "".join(e["text"] for e in all_of("input_transcript") if start_ms <= e["t_ms"] < end_ms)
    if not text.strip():
        raise SystemExit(f"no input transcription between {start_ms} and {end_ms} ms")
    return " ".join(text.split())


def user_clips(side: dict, all_of) -> dict:
    clips = {c["label"]: c for c in side["user_clips"]}
    for label, c in clips.items():
        assert c["sent_start_ms"] == all_of("user_audio_start", label=label)[0]["t_ms"], label
        assert (ROOT / c["source"]).exists(), c["source"]
    return clips


def load_lockout() -> dict:
    ev = load_events(LOCK_JSONL, LOCK_RUN)
    all_of, one = finder(ev, f"{LOCK_JSONL.name} run {LOCK_RUN}")
    side = json.loads(LOCK_SIDECAR.read_text(encoding="utf-8"))
    bh = one("blackhole_start")
    fails = all_of("reconnect_attempt_failed")
    assert fails and all((f.get("close") or {}).get("code") == 1011 for f in fails), "not all 1011"
    assert not all_of("resumed"), "a resume was accepted"
    commits = all_of("service_committed")
    assert len(commits) == 1, "expected one commit"
    assert not all_of("model_transcript") and not side["chunks"], \
        "the model spoke; this clip says it did not"
    call = one("tool_call_received")
    run_cfg = one("run_start")
    assert run_cfg.get("voice_dir") == "assets/audio/af_heart", run_cfg.get("voice_dir")
    return {
        "book_ms": one("user_audio_start", label="book_request")["t_ms"],
        "book_end_ms": one("user_audio_end", label="book_request")["t_ms"],
        "book_text": heard(all_of, one("user_audio_start", label="book_request")["t_ms"],
                           call["t_ms"] + 1),
        "tool_call_ms": call["t_ms"], "call_id": call["call_id"],
        "job_ms": one("service_job_started")["t_ms"],
        "latency_ms": int(round(one("service_job_started")["latency_s"] * 1000)),
        "bh_ms": bh["t_ms"],
        "detect_ms": one("link_lost_detected")["t_ms"],
        "attempts": [{"n": f["attempt"], "start": f["start_ms"], "end": f["close"]["at_ms"],
                      "ms": f["ms_to_error"], "code": f["close"]["code"],
                      "reason": f["close"]["reason"]} for f in fails],
        "period_s": float(run_cfg["round3"]["resume"].split("every ")[1].split("s")[0]),
        "bh_max_s": float(run_cfg["round3"]["resume"].split("up to ")[1].split("s")[0]),
        "committed_ms": commits[0]["t_ms"], "confirmation": commits[0]["confirmation_id"],
        "slot": commits[0]["slot"],
        "end_ms": one("lockout")["t_ms"],
        "clips": user_clips(side, all_of),
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
    book = one("user_audio_start", label="book_request")
    assert len(commits) == 1, "expected one commit"
    assert len(all_of("tool_call_received")) == 1, "a call was re-issued; the clip says it was not"
    assert one("recovery_done")["mode"] == "new_session", "not a fallback run"
    assert all("1011" in f["error"] for f in fails), "a window attempt was not a 1011"
    assert one("run_start").get("voice_dir") == "assets/audio/af_heart"
    answer_ms = next(e["t_ms"] for e in texts if e["t_ms"] >= ask["t_ms"])
    return {
        "book_ms": book["t_ms"],
        "book_end_ms": one("user_audio_end", label="book_request")["t_ms"],
        "book_text": heard(all_of, book["t_ms"], call["t_ms"] + 1),
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
        "ask_text": heard(all_of, ask["t_ms"], answer_ms + 1),
        "answer_ms": answer_ms, "answer": answer,
        "interrupted_ms": sorted(side["events"]["interrupted_ms"]),
        "side": side, "clips": user_clips(side, all_of),
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
PILL_Y = 50
F_PILL = ("bold", 16)


def pill_box(label: str) -> tuple[float, float, float, float]:
    w = tw(label, font(*F_PILL)) + 24
    return (W - M - 4 - w, PILL_Y, W - M - 4, PILL_Y + 24)


def draw_header(c: Canvas, t_ms: float, subtitle: str, speed: str) -> None:
    """speed: the pill under the clock: "real time", "time compressed xN" or "still frame"."""
    c.text(M + 4, 16, TITLE, font("bold", 30), INK)
    box = pill_box(speed)
    assert M + 4 + tw(subtitle, font("regular", 18)) < box[0] - 16, f"subtitle too long: {subtitle}"
    c.text(M + 4, 52, subtitle, font("regular", 18), INK_2)
    clock = f"{t_ms / 1000:.2f} s"
    fc = font("mono_bold", 34)
    c.text(W - M - 4, 14, clock, fc, INK, anchor="ra")
    c.text(W - M - 4 - tw(clock, fc) - 10, 26, "t =", font("regular", 22), INK_2, anchor="ra")
    fast = speed.startswith("time compressed")
    c.rrect(box, 12, fill=ORANGE if fast else PANEL, outline=None if fast else BORDER, width=1)
    c.text((box[0] + box[2]) / 2, box[1] + 12, speed, font(*F_PILL), WHITE if fast else INK_2,
           anchor="mm")


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


def user_msg(at_ms: float, text: str) -> dict:
    return {"kind": "user", "at": at_ms, "role": f"User (voice), {secs(at_ms)}", "text": text}


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


def attempt_rows(d: dict, t_ms: float) -> list[tuple[str, tuple]]:
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


# -- events strip: a linear time axis, or a broken one (real time left of the break,
# time compressed right of it, each on its own share of the width)
class Axis:
    def __init__(self, t0: float, t1: float, brk: float | None = None, frac: float = 0.5,
                 left_every_s: int = 1, left_label_every: int = 2, right_every_s: int = 10) -> None:
        self.t0, self.t1, self.brk, self.frac = t0, t1, brk, frac
        self.left_every_s, self.left_label_every, self.right_every_s = (
            left_every_s, left_label_every, right_every_s)
        self.xb = AX_X0 + (AX_X1 - AX_X0) * frac if brk is not None else None

    def x(self, t_ms: float) -> float:
        t_ms = min(max(t_ms, self.t0), self.t1)
        if self.brk is None:
            return AX_X0 + (AX_X1 - AX_X0) * (t_ms - self.t0) / (self.t1 - self.t0)
        if t_ms <= self.brk:
            return AX_X0 + (self.xb - AX_X0) * (t_ms - self.t0) / (self.brk - self.t0)
        return self.xb + (AX_X1 - self.xb) * (t_ms - self.brk) / (self.t1 - self.brk)

    def ticks(self) -> list[tuple[float, str | None]]:
        """(t_ms, label or None) for each axis tick."""
        out = []
        left_end = self.t1 if self.brk is None else self.brk
        s = math.ceil(self.t0 / 1000 / self.left_every_s) * self.left_every_s
        while s * 1000 <= left_end:
            lab = f"{s} s" if (s // self.left_every_s) % self.left_label_every == 0 else None
            out.append((s * 1000, lab))
            s += self.left_every_s
        if self.brk is not None:
            s = math.ceil(self.brk / 1000 / self.right_every_s) * self.right_every_s
            while s * 1000 <= self.t1:
                far = self.x(s * 1000) - self.xb > 60   # no label crowding the break
                out.append((s * 1000, f"{s} s" if far else None))
                s += self.right_every_s
        return out


def strip_layout(ticks: list[tuple], axis: Axis):
    f_lab = font(*F_LAB)
    boxes, leaders = [], []
    for label, at, _, row, side, _ in ticks:
        x, ly = axis.x(at), ROW_Y[row]
        w = tw(label, f_lab)
        boxes.append((label, (x + 8, ly, x + 8 + w, ly + 21) if side == "right" else (x - 8 - w, ly, x - 8, ly + 21)))
        leaders.append((label, (x - 1, ly + 2, x + 1, AX_Y)))
    return boxes, leaders


def overlaps(ticks: list[tuple], axis: Axis, axis_note: str, extra: list[tuple] = ()) -> list[str]:
    def hit(a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
    boxes, leaders = strip_layout(ticks, axis)
    boxes += list(extra)
    fe = font("bold", 22)
    head_w = tw("Events", fe) + 10 + tw(axis_note, font("regular", 18))
    boxes.append(("header", (STRIP[0] + 20, STRIP[1] + 12, STRIP[0] + 20 + head_w, STRIP[1] + 36)))
    bad = [f"{a!r} x {b!r}" for i, (a, ba) in enumerate(boxes) for b, bb in boxes[i + 1:] if hit(ba, bb)]
    bad += [f"{a!r} x leader of {b!r}" for a, ba in boxes for b, lb in leaders if a != b and hit(ba, lb)]
    bad += [f"{a!r} outside the strip" for a, ba in boxes if ba[0] < STRIP[0] + 8 or ba[2] > STRIP[2] - 8]
    play = (AX_X0 - 7, AX_Y - 22, AX_X1 + 7, AX_Y - 10)
    bad += [f"{a!r} x playhead path" for a, ba in boxes if hit(ba, play)]
    # axis tick labels must not collide with each other
    fl = font("regular", 17)
    labs = [(lab, (axis.x(t) - tw(lab, fl) / 2, axis.x(t) + tw(lab, fl) / 2)) for t, lab in axis.ticks() if lab]
    bad += [f"axis labels {a!r} x {b!r}" for i, (a, (a0, a1)) in enumerate(labs)
            for b, (b0, b1) in labs[i + 1:] if a0 < b1 + 6 and b0 < a1 + 6]
    return bad


def draw_strip(c: Canvas, ticks: list[tuple], t_ms: float, axis: Axis, playhead: bool = True,
               axis_note: str = "seconds since session start", marks: list[tuple] = ()) -> None:
    c.rrect(STRIP, 12, fill=PANEL, outline=BORDER, width=1)
    c.text(STRIP[0] + 20, STRIP[1] + 12, "Events", font("bold", 22), INK)
    c.text(STRIP[0] + 20 + tw("Events", font("bold", 22)) + 10, STRIP[1] + 16, axis_note,
           font("regular", 18), INK_2)
    now_x = axis.x(t_ms)
    c.line([(AX_X0, AX_Y), (AX_X1, AX_Y)], TRACK, width=3)
    c.line([(AX_X0, AX_Y), (now_x, AX_Y)], INK_3, width=3)
    for t, lab in axis.ticks():
        x = axis.x(t)
        c.line([(x, AX_Y + 4), (x, AX_Y + 9)], INK_3, width=1)
        if lab:
            c.text(x, AX_Y + 12, lab, font("regular", 17), INK_3, anchor="ma")
    if axis.xb is not None:   # the break: two slanted strokes across the axis
        xb = axis.xb
        c.line([(xb - 5, AX_Y), (xb + 5, AX_Y)], PANEL, width=6)
        c.line([(xb - 8, AX_Y + 8), (xb - 2, AX_Y - 8)], INK_2, width=2)
        c.line([(xb + 2, AX_Y + 8), (xb + 8, AX_Y - 8)], INK_2, width=2)
    for at, color in marks:   # refused resumes without a label: a dot on the axis
        if t_ms >= at:
            c.dot(axis.x(at), AX_Y, 6, color, ring=PANEL)
    shown = [tk for tk in ticks if t_ms >= tk[1]]
    for label, at, color, row, side, _ in shown:
        x, ly = axis.x(at), ROW_Y[row]
        c.line([(x, AX_Y), (x, ly + 2)], color, width=2)
        if side == "right":
            c.text(x + 8, ly, label, font(*F_LAB), INK)
        else:
            c.text(x - 8, ly, label, font(*F_LAB), INK, anchor="ra")
    for label, at, color, row, side, marker in shown:
        x = axis.x(at)
        if marker == "ring":
            c.dot(x, AX_Y, 9, PANEL)
            c.ring(x, AX_Y, 9, color, width=2)
        else:
            c.dot(x, AX_Y, 6, color, ring=PANEL)
    if playhead:
        c.poly([(now_x - 7, AX_Y - 20), (now_x + 7, AX_Y - 20), (now_x, AX_Y - 10)], INK)


# ---------------------------------------------------------------- without recovery
LOCK_SUB = "Without recovery. B1 timing, recorded for this clip (not in the measured tables)."
LOCK_NOTE = f"seconds since session start; right of the break, time compressed ×{FAST}"


def lock_ticks(d: dict) -> list[tuple]:
    a = d["attempts"]
    return [
        (f"toolCall {secs(d['tool_call_ms'])}", d["tool_call_ms"], BLUE, 4, "left", "dot"),
        (f"network lost {secs(d['bh_ms'])}", d["bh_ms"], ORANGE, 3, "left", "dot"),
        (f"loss detected {secs(d['detect_ms'])}", d["detect_ms"], INK, 2, "left", "dot"),
        (f"resume #1 refused (1011) {secs(a[0]['end'])}", a[0]["end"], ORANGE, 1, "right", "dot"),
        (f"committed {secs(d['committed_ms'])}", d["committed_ms"], GREEN, 2, "right", "dot"),
        (f"resume #{a[-1]['n']} refused (1011) {secs(a[-1]['end'])}", a[-1]["end"], ORANGE, 1,
         "left", "dot"),
    ]


def lock_marks(d: dict) -> list[tuple]:
    return [(x["end"], ORANGE) for x in d["attempts"][1:-1]]


def lock_rows(d: dict, t_ms: float) -> list[tuple[str, tuple]]:
    """The last three attempts, then the next one (or the end of the 60 s loop)."""
    rows = attempt_rows(d, t_ms)[-3:]
    if t_ms >= d["attempts"][0]["end"]:
        nxt = next((a for a in d["attempts"] if a["start"] > t_ms), None)
        if nxt is not None:
            rows.append((f"next attempt at {secs(nxt['start'])} (every {d['period_s']:.0f} s)", INK_3))
        elif t_ms >= d["attempts"][-1]["end"]:
            rows.append((f"no attempt after +{d['bh_max_s']:.0f} s: the clip's session stops here",
                         INK_3))
    return rows


def lock_conversation(d: dict, t_ms: float, c: Canvas) -> None:
    note, col = silence_note(t_ms, d["book_end_ms"], None)
    draw_panel(c, LEFT, "Conversation", note, col)
    draw_messages(c, [user_msg(d["book_ms"], d["book_text"])], t_ms)


def render_lock(d: dict, t_ms: float, axis: Axis, speed: str) -> Image.Image:
    c = Canvas()
    draw_header(c, t_ms, LOCK_SUB, speed)
    lock_conversation(d, t_ms, c)
    draw_service(c, d, t_ms, "not reported")
    if t_ms < d["bh_ms"]:
        status = ("connected (connection 1, session resumption on)", BLUE)
    elif t_ms < d["detect_ms"]:
        status = ("packets dropped both ways, nothing closed", ORANGE)
    else:
        status = (f"lost: detected at {secs(d['detect_ms'])}, resuming with the handle", ORANGE)
    draw_connection(c, d, t_ms, status, lock_rows(d, t_ms))
    draw_strip(c, lock_ticks(d), t_ms, axis, axis_note=LOCK_NOTE, marks=lock_marks(d))
    return c.frame()


END_BIG = ["After 15 minutes in our tests:", "still refused,", "the user is never told."]


def end_small(d: dict) -> str:
    return (f"The 15 minutes come from {ROUND3}. This session only tried for "
            f"{d['bh_max_s']:.0f} s after the loss. Here too the model said nothing after its tool "
            f"call; the booking was committed {(d['committed_ms'] - d['bh_ms']) / 1000:.1f} s "
            f"after the loss.")


def render_end(d: dict, axis: Axis, t_ms: float) -> Image.Image:
    c = Canvas()
    draw_header(c, t_ms, f"Without recovery. End of the clip's session, {d['bh_max_s']:.0f} s "
                         "resume loop. Below: what round 3 found.", "still frame")
    lock_conversation(d, t_ms, c)
    x0, room = LEFT[0] + 20, LEFT[2] - LEFT[0] - 40
    size = min(fit(s, "bold", 30, room).size for s in END_BIG) // SS
    y = CONV_TOP + message_blocks([user_msg(d["book_ms"], d["book_text"])])[0]["h"] + 18
    for s in END_BIG:
        c.text(x0, y, s, font("bold", size), INK)
        y += size + 8
    y += 14
    for s in wrap(end_small(d), font("regular", 17), room):
        c.text(x0, y, s, font("regular", 17), INK_2)
        y += 22
    assert y < LEFT[3] - 8, "end text overflows the panel"
    draw_service(c, d, t_ms, "never reported")
    a = d["attempts"]
    lo, hi = min(x["ms"] for x in a), max(x["ms"] for x in a)
    rows = [(f"resume #1 to #{len(a)}, from {secs(a[0]['start'])} to {secs(a[-1]['start'])}", INK),
            (f"all {len(a)} closed {a[0]['code']} “{a[0]['reason']}”", INK),
            (f"each after {lo} to {hi} ms; none hung, none accepted", INK),
            ("the old socket is still open on the client", INK_2)]
    draw_connection(c, d, t_ms, (f"locked out: no resume accepted in {d['bh_max_s']:.0f} s", ORANGE),
                    rows)
    draw_strip(c, lock_ticks(d), t_ms, axis, playhead=False, axis_note=LOCK_NOTE,
               marks=lock_marks(d))
    return c.frame()


# ---------------------------------------------------------------- with recovery
REC_SUB = "With recovery. BR1 timing, recorded for this clip (not in the measured tables)."


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
        user_msg(d["book_ms"], d["book_text"]),
        {"kind": "note", "at": d["restore_ms"],
         "role": f"App to the new session (text), {secs(d['restore_ms'])}: last turn + note",
         "text": excerpt},
        {"kind": "model", "at": d["reply_ms"], "role": f"Model (spoken reply), {secs(d['reply_ms'])}",
         "text": d["reply"], "foot": foot, "foot_at": cut_ms or 0},
        user_msg(d["ask_ms"], d["ask_text"]),
        {"kind": "model", "at": d["answer_ms"], "role": f"Model (spoken reply), {secs(d['answer_ms'])}",
         "text": d["answer"]},
    ]


def render_rec(d: dict, t_ms: float, axis: Axis, cut_ms: float | None,
               first_audio_ms: float) -> Image.Image:
    c = Canvas()
    draw_header(c, t_ms, REC_SUB, "real time")
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
    draw_connection(c, d, t_ms, status, attempt_rows(d, t_ms))
    draw_strip(c, rec_ticks(d), t_ms, axis, marks=[(x["end"], ORANGE) for x in d["attempts"][1:]])
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
    lock_a_end = max(L["attempts"][0]["end"], L["committed_ms"]) + LOCK_A_TAIL_MS
    lock_b_end = L["attempts"][-1]["end"] + LOCK_B_TAIL_MS
    lock_axis = Axis(0, lock_b_end, brk=lock_a_end, frac=0.5)
    rec_t0, rec_t1 = REC_FROM_MS, model_end + REC_TAIL_MS
    rec_axis = Axis(rec_t0, rec_t1)
    lock_clip_end = L["clips"]["book_request"]["sent_start_ms"] + L["clips"]["book_request"]["duration_ms"]
    assert lock_clip_end < lock_a_end, "user audio would fall in the compressed part"

    print(f"without recovery: book {L['book_ms']} ({L['book_text']!r}), toolCall {L['tool_call_ms']}, "
          f"loss {L['bh_ms']}, detected {L['detect_ms']}, commit {L['committed_ms']}, "
          f"attempts {len(L['attempts'])} ends {[x['end'] for x in L['attempts']]}, real time to "
          f"{lock_a_end} ms, compressed x{FAST} to {lock_b_end} ms, lockout {L['end_ms']}")
    print(f"with recovery: book {R['book_ms']} ({R['book_text']!r}), loss {R['bh_ms']}, detected "
          f"{R['detect_ms']}, window end {R['window_end_ms']}, commit {R['committed_ms']}, setupComplete "
          f"{R['setup_ms']}, restore {R['restore_ms']}, model {R['reply_ms']} ({R['reply']!r}), ask "
          f"{R['ask_ms']} ({R['ask_text']!r}), interrupted {R['interrupted_ms']}, answer "
          f"{R['answer_ms']} ({R['answer']!r}); model audio "
          + ", ".join(f"{s:.0f}-{s + len(x) * 1000 / SR:.0f}" for s, x in segments))

    bad = (overlaps(lock_ticks(L), lock_axis, LOCK_NOTE)
           + overlaps(rec_ticks(R), rec_axis, "seconds since session start"))
    for b in bad:
        print(f"OVERLAP in the events strip: {b}")
    if bad:
        raise SystemExit("fix the strip layout first")

    cards = {
        "title": render_card([("The connection drops during a booking", 46, INK),
                              ("First without recovery, then with it.", 26, INK_2),
                              (f"User voice: {VOICE} (open-weight TTS, Apache-2.0), sent to the "
                               "model as live audio.", 20, INK_3)]),
        "card_with": render_card([("With recovery", 46, INK)]),
    }
    speed_b = f"time compressed ×{FAST}"
    if a.preview:
        a.preview.mkdir(parents=True, exist_ok=True)
        for k, im in cards.items():
            im.save(a.preview / f"card-{k}.png")
        for t in (L["bh_ms"] + 500, L["committed_ms"] + 300, lock_a_end - 1):
            render_lock(L, t, lock_axis, "real time").save(a.preview / f"lock-{int(t):05d}.png")
        for t in (30_000, L["attempts"][-1]["end"] + 200):
            render_lock(L, t, lock_axis, speed_b).save(a.preview / f"lock-{int(t):05d}.png")
        render_end(L, lock_axis, lock_b_end).save(a.preview / "end.png")
        for t in (R["detect_ms"] + 1200, R["restore_ms"] + 300, R["reply_ms"] + 1500,
                  R["ask_ms"] + 600, R["answer_ms"] + 1500, rec_t1):
            render_rec(R, t, rec_axis, cut, first_audio).save(a.preview / f"rec-{int(t):05d}.png")
        print(f"stills in {a.preview}")
        return

    n_card, n_end = (int(round(s * FPS)) for s in (CARD_S, END_S))
    n_a = math.ceil(lock_a_end / 1000 * FPS)
    n_b = math.ceil((lock_b_end - lock_a_end) / 1000 / FAST * FPS)
    n_rec = math.ceil((rec_t1 - rec_t0) / 1000 * FPS)
    n_hold = int(round(HOLD_S * FPS))
    parts = [("title", n_card), ("without", n_a), ("compressed", n_b), ("end", n_end),
             ("card_with", n_card), ("with", n_rec + n_hold)]
    total = sum(n for _, n in parts)
    print("frames: " + " + ".join(f"{k} {n}" for k, n in parts) + f" = {total} ({total / FPS:.2f} s)")

    # audio: one track, one gain; the user clips are the files the harness sent
    track = np.zeros(total * SPF, dtype=np.float32)
    off = n_card * SPF
    lock_mix = np.zeros(n_a * SPF, dtype=np.float32)
    for c in L["clips"].values():
        place(lock_mix, decode(ROOT / c["source"]), c["sent_start_ms"])
    track[off: off + len(lock_mix)] += lock_mix
    rec_off_frames = n_card + n_a + n_b + n_end + n_card
    off = rec_off_frames * SPF
    rec_mix = np.zeros((n_rec + n_hold) * SPF, dtype=np.float32)
    for c in R["clips"].values():
        place(rec_mix, decode(ROOT / c["source"]), c["sent_start_ms"] - rec_t0)
    for start, seg in segments:
        place(rec_mix, seg, start - rec_t0)
    track[off: off + len(rec_mix)] += rec_mix
    peak = float(np.abs(track).max())
    gain = PEAK / peak
    track *= gain
    print(f"audio: one gain {20 * math.log10(gain):+.2f} dB (raw peak {peak:.3f} -> -1 dBFS); "
          f"voiced rms {voiced_rms_db(track):.1f} dBFS")
    placements = ([(f"without: user {k} ({c['source']})", n_card / FPS + c["sent_start_ms"] / 1000)
                   for k, c in L["clips"].items()]
                  + [(f"with: user {k} ({c['source']})", rec_off_frames / FPS + c["sent_start_ms"] / 1000)
                     for k, c in R["clips"].items()]
                  + [("with: model audio, first chunk", rec_off_frames / FPS + first_audio / 1000),
                     ("with: model audio, end", rec_off_frames / FPS + model_end / 1000)])
    print("audio placements (s in the clip): " + "; ".join(f"{k} {v:.3f}" for k, v in placements))

    OUT_MP4.parent.mkdir(parents=True, exist_ok=True)
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
            elif kind == "without":
                for i in range(n):
                    t = min(i * 1000 / FPS, lock_a_end)
                    proc.stdin.write(render_lock(L, t, lock_axis, "real time").tobytes())
            elif kind == "compressed":
                for i in range(n):
                    t = min(lock_a_end + i * 1000 * FAST / FPS, lock_b_end)
                    proc.stdin.write(render_lock(L, t, lock_axis, speed_b).tobytes())
            elif kind == "end":
                buf = render_end(L, lock_axis, lock_b_end).tobytes()
                for _ in range(n):
                    proc.stdin.write(buf)
            elif kind == "with":
                for i in range(n):
                    t = min(rec_t0 + i * 1000 / FPS, rec_t1)
                    proc.stdin.write(render_rec(R, t, rec_axis, cut, first_audio).tobytes())
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
