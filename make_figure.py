"""The lockout and the recovery in one figure.

    uv run --with matplotlib python make_figure.py

Top: the five real-packet-loss runs where the old path never came back (round 3, B1 x3
with the model idle, B2 x2 with the model speaking). Time since the loss on a log axis
up to 15 minutes; one orange tick per refused resume, the booking commit, and the last
segment the server's TCP sent on the dead flow (a retransmission).
Bottom: the four stage-2 recovery runs (BR1): detection, the 4 s resume window and its
refusals, the commit, the new session's setupComplete and its first model audio.

Every value is read from results/B1.jsonl, results/B2.jsonl and results/BR1.jsonl;
nothing is typed in. The script checks what the figure claims (every refusal is a close
1011, no resume was accepted, one commit per run, the last server segment is a
retransmission) and exits with status 1 if a check fails or two labels overlap.
Writes results/figures/lockout.png (1600x900).
"""

import json
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle, Polygon, Rectangle  # noqa: E402

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
OUT = RESULTS / "figures" / "lockout.png"

# Palette: the first three categorical slots of the reference palette (they pass the
# all-pairs CVD and normal-vision checks on this surface) plus grey chrome. Aqua is
# under 3:1 on the surface, so every aqua mark is also named by a label.
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
RULE = "#e1e0d9"
REFUSED, MODEL, BOOKED = "#eb6834", "#2a78d6", "#1baf7a"   # orange, blue, aqua
BAND = "#fbe3d8"                                           # orange tint: the resume window

FOOTER = ("gemini-3.8-live, Gemini API, 2026-10-03. "
          "github.com/frontier-on-cloud/gemini-live-resume-test")

W, H = 1600, 900
LX = 64                       # left margin, row labels
X0, X1 = 300, 1250            # plot area, both panels
A_ROWS = [252, 292, 332, 372, 412]
A_AXIS = 442
A_MIN_S, A_MAX_S = 0.5, 900.0  # log axis: 0.5 s to 15 min
B_ROWS = [632, 670, 708, 746]
B_AXIS = 776
B_MAX_S = 8.0

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "text.color": INK,
})


# ------------------------------------------------------------------ data --


def runs_of(name):
    runs = {}
    for line in (RESULTS / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            runs.setdefault(e["run"], []).append(e)
    return runs


def first(events, name):
    return next(e for e in events if e["event"] == name)


def lockout_runs(problems):
    """B1 and B2: every attempt refused for 15 min. Times in s since the blackhole."""
    rows = []
    for name in ("B1", "B2"):
        for run, ev in sorted(runs_of(name).items()):
            bh = first(ev, "blackhole_start")["t_ms"]
            fails = [e for e in ev if e["event"] == "reconnect_attempt_failed"]
            refused = []
            for e in fails:
                close = e.get("close") or {}
                if close.get("code") != 1011 or e.get("hung"):
                    problems.append(f"{name} run {run} attempt {e['attempt']}: not a 1011 close")
                refused.append((close.get("at_ms", e["t_ms"]) - bh) / 1000)
            if any(e["event"] in ("resumed", "reconnected") for e in ev):
                problems.append(f"{name} run {run}: a resume was accepted")
            if not any(e["event"] == "lockout" for e in ev):
                problems.append(f"{name} run {run}: no lockout event")
            commits = [e["t_ms"] for e in ev if e["event"] == "service_committed"]
            if len(commits) != 1:
                problems.append(f"{name} run {run}: {len(commits)} commits")
            s2c = [e for e in ev if e["event"] == "old_flow_packet" and e["dir"] == "s2c"]
            last = s2c[-1]
            if not any(p["seq"] == last["seq"] and p["seq_end"] == last["seq_end"] for p in s2c[:-1]):
                problems.append(f"{name} run {run}: last server segment is not a retransmission")
            if any(p.get("flags", "").count("R") or "F" in p.get("flags", "") for p in s2c):
                problems.append(f"{name} run {run}: RST or FIN from the server")
            rows.append({"label": f"{name} run {run}", "refused": refused,
                         "commit": (commits[0] - bh) / 1000,
                         "last_retx": last["since_blackhole_ms"] / 1000,
                         "end": first(ev, "lockout")["since_blackhole_ms"] / 1000})
    return rows


def recovery_runs(problems):
    """BR1: times in s since the blackhole."""
    rows = []
    for run, ev in sorted(runs_of("BR1").items()):
        bh = first(ev, "blackhole_start")["t_ms"]
        fails = [e for e in ev if e["event"] == "resume_attempt_failed"]
        if not fails or any("1011" not in e["error"] for e in fails):
            problems.append(f"BR1 run {run}: a window attempt was not refused with 1011")
        restore = first(ev, "restore_sent")["t_ms"]
        ask = first(ev, "ask_queued")["t_ms"]
        new_conn = first(ev, "new_session_open")
        audio = [e["t_ms"] for e in ev if e["event"] == "audio_start" and e["conn"] != 1
                 and e["t_ms"] >= restore]
        said = "".join(e["text"] for e in ev if e["event"] == "model_transcript"
                       and restore <= e["t_ms"] < ask).strip()
        commits = [e["t_ms"] for e in ev if e["event"] == "service_committed"]
        if len(commits) != 1:
            problems.append(f"BR1 run {run}: {len(commits)} commits")
        if not any(w in said.lower() for w in ("booked", "confirmed")):
            problems.append(f"BR1 run {run}: first reply does not say booked/confirmed: {said!r}")
        rows.append({"label": f"BR1 run {run}",
                     "detect": (first(ev, "link_lost_detected")["t_ms"] - bh) / 1000,
                     "window_end": (first(ev, "resume_window_end")["t_ms"] - bh) / 1000,
                     "refused": [(e["start_ms"] + e["ms_to_error"] - bh) / 1000 for e in fails],
                     "commit": (commits[0] - bh) / 1000,
                     "setup": (new_conn["t_ms"] - bh) / 1000,
                     "audio": (audio[0] - bh) / 1000,
                     "said": said})
    return rows


# --------------------------------------------------------------- drawing --

PT = 72 / 100                 # matplotlib font sizes are points; the canvas is 100 px per inch


def xa(s):
    """Panel A: log scale, 0.5 s at X0 to 900 s (15 min) at X1."""
    return X0 + (X1 - X0) * math.log10(max(s, A_MIN_S) / A_MIN_S) / math.log10(A_MAX_S / A_MIN_S)


def xb(s):
    """Panel B: linear, 0 s (the loss) at X0 to 8 s at X1."""
    return X0 + (X1 - X0) * s / B_MAX_S


def main():
    problems = []
    lock = lockout_runs(problems)
    rec = recovery_runs(problems)
    n_refused = sum(len(r["refused"]) for r in lock)
    last_try = max(max(r["refused"]) for r in lock)
    audio_lo, audio_hi = min(r["audio"] for r in rec), max(r["audio"] for r in rec)
    det_lo, det_hi = min(r["detect"] for r in rec), max(r["detect"] for r in rec)
    n_win = sum(len(r["refused"]) for r in rec)
    if any(r["commit"] < A_MIN_S for r in lock):
        problems.append("a commit falls before the start of the log axis")

    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor=SURFACE)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis("off")
    labels = []
    renderer = fig.canvas.get_renderer()

    def text(x, y, s, px=24, **kw):
        """Text with its size in canvas pixels (cap height to descender, as rendered)."""
        kw.setdefault("va", "center")
        t = ax.text(x, y, s, fontsize=px * PT, **kw)
        labels.append((s, t))
        return t

    def width(t):
        return t.get_window_extent(renderer).width

    def tick(x, y, h, color, lw=2.0):
        ax.plot([x, x], [y - h / 2, y + h / 2], color=color, lw=lw * PT, solid_capstyle="butt",
                zorder=3)

    def leader(x, y0, y1):
        ax.plot([x, x], [y0, y1], color=MUTED, lw=1.5 * PT, zorder=2)

    def diamond(x, y, r=11):
        ax.add_patch(Polygon([(x, y - r), (x + r, y), (x, y + r), (x - r, y)], closed=True,
                             facecolor=BOOKED, edgecolor=SURFACE, lw=2 * PT, zorder=5))

    def ring(x, y, r=10):
        ax.add_patch(Circle((x, y), r, facecolor=SURFACE, edgecolor=INK2, lw=3 * PT, zorder=5))

    def dot(x, y, r=10):
        ax.add_patch(Circle((x, y), r, facecolor=MODEL, edgecolor=SURFACE, lw=2 * PT, zorder=5))

    text(LX, 50, "A silent loss: every resume refused for 15 minutes", px=52, fontweight="bold")

    # ---- legend, shared by both panels
    ly, lx = 108, LX
    keys = [(lambda x: tick(x + 4, ly, 28, REFUSED, 3), 14, "resume refused (1011)"),
            (lambda x: diamond(x + 11, ly), 30, "booking committed"),
            (lambda x: ring(x + 10, ly), 28, "server's last TCP retransmission"),
            (lambda x: dot(x + 10, ly), 28, "first model audio"),
            (lambda x: ax.add_patch(Rectangle((x, ly - 13), 30, 26, facecolor=BAND,
                                              edgecolor="none", zorder=1)), 40, "resume window")]
    for draw, gap, label in keys:
        draw(lx)
        t = text(lx + gap, ly, label, px=24, color=INK2)
        lx += gap + width(t) + 34

    # ---- panel A: the lockout (log time)
    t = text(LX, 172, "Without recovery", px=40, fontweight="bold")
    text(LX + width(t) + 22, 174, f"real packet loss, 5 runs, {n_refused} of {n_refused} resumes "
                                  "refused", px=26, color=INK2)
    for y, r in zip(A_ROWS, lock):
        ax.plot([X0, X1], [y, y], color=RULE, lw=2 * PT, zorder=1)
        text(LX, y, r["label"], px=24, color=INK2)
        for s in r["refused"]:
            tick(xa(s), y, 28, REFUSED, 1.6)
        diamond(xa(r["commit"]), y)
        ring(xa(r["last_retx"]), y)
    c0, top = lock[0], A_ROWS[0] - 38
    text(xa(c0["commit"]) - 10, top, "booking committed", px=24, ha="left")
    leader(xa(c0["commit"]), top + 13, A_ROWS[0] - 13)
    text(xa(c0["last_retx"]) + 10, top, "server's last TCP retransmission", px=24, ha="right")
    leader(xa(c0["last_retx"]), top + 13, A_ROWS[0] - 12)
    mid = (A_ROWS[0] + A_ROWS[-1]) / 2
    text(X1 + 34, mid - 46, "no accepted", px=36, fontweight="bold")
    text(X1 + 34, mid, "resume", px=36, fontweight="bold")
    text(X1 + 34, mid + 44, "user never told", px=26, color=INK2)
    ax.plot([X0, X1], [A_AXIS, A_AXIS], color=MUTED, lw=1.5 * PT, zorder=1)
    for s, lab in ((1, "1 s"), (10, "10 s"), (60, "1 min"), (300, "5 min"), (900, "15 min")):
        ax.plot([xa(s), xa(s)], [A_AXIS, A_AXIS + 7], color=MUTED, lw=1.5 * PT)
        text(xa(s), A_AXIS + 25, lab, px=22, color=INK2, ha="center")
    text(LX, A_AXIS + 25, "since the loss (log)", px=20, color=MUTED)

    # ---- panel B: the recovery (linear time)
    t = text(LX, 540, "With recovery", px=40, fontweight="bold")
    text(LX + width(t) + 22, 542, f"new session after the window, first model audio "
                                  f"{audio_lo:.1f} to {audio_hi:.1f} s after the loss", px=26,
         color=INK2)
    for y, r in zip(B_ROWS, rec):
        ax.plot([X0, xb(r["detect"])], [y, y], color=RULE, lw=2 * PT, zorder=1)
        ax.add_patch(Rectangle((xb(r["detect"]), y - 13), xb(r["window_end"]) - xb(r["detect"]),
                               26, facecolor=BAND, edgecolor="none", zorder=1))
        ax.plot([xb(r["window_end"]), xb(r["audio"])], [y, y], color=RULE, lw=2 * PT, zorder=1)
        text(LX, y, r["label"], px=24, color=INK2)
        tick(xb(r["detect"]), y, 26, INK2, 2.5)
        for s in r["refused"]:
            tick(xb(s), y, 26, REFUSED, 3)
        diamond(xb(r["commit"]), y)
        tick(xb(r["setup"]), y, 26, MODEL, 2.5)
        dot(xb(r["audio"]), y)
    r0, top = rec[0], B_ROWS[0] - 38
    ax.plot([X0, X0], [top + 13, B_ROWS[-1] + 14], color=INK2, lw=1.5 * PT, zorder=1)
    text(X0 - 6, top, "loss", px=24, ha="left")
    text(xb(det_lo) + 8, top, "detected", px=24, ha="right")
    leader(xb(r0["detect"]), top + 13, B_ROWS[0] - 14)
    text(xb(r0["refused"][0]) - 6, top, f"{n_win // len(rec)} resumes refused", px=24, ha="left")
    leader(xb(r0["refused"][0]), top + 13, B_ROWS[0] - 14)
    text(xb(r0["setup"]) + 6, top, "new session", px=24, ha="right")
    leader(xb(r0["setup"]), top + 13, B_ROWS[0] - 14)
    text(xb(r0["audio"]) - 10, top, "speaks", px=24, ha="left")
    leader(xb(r0["audio"]), top + 13, B_ROWS[0] - 11)
    midb = (B_ROWS[0] + B_ROWS[-1]) / 2
    text(X1 + 34, midb - 46, "model says", px=36, fontweight="bold")
    text(X1 + 34, midb, "it is booked", px=36, fontweight="bold")
    text(X1 + 34, midb + 44, "or confirmed, 4 of 4", px=26, color=INK2)
    ax.plot([X0, X1], [B_AXIS, B_AXIS], color=MUTED, lw=1.5 * PT, zorder=1)
    for s in range(0, int(B_MAX_S) + 1):
        ax.plot([xb(s), xb(s)], [B_AXIS, B_AXIS + 7], color=MUTED, lw=1.5 * PT)
        text(xb(s), B_AXIS + 25, f"{s} s", px=22, color=INK2, ha="center")
    text(LX, B_AXIS + 25, "since the loss", px=20, color=MUTED)

    text(LX, 862, FOOTER, px=24, color=INK2)

    # ---- checks
    if not all(r["refused"] for r in rec) or n_win != 4 * len(rec):
        problems.append(f"BR1: {n_win} window attempts, expected 4 per run")
    print(f"lockout: {len(lock)} runs, {n_refused} refusals (all 1011), last try "
          f"+{last_try:.1f} s; commits +" + ", +".join(f"{r['commit']:.1f}" for r in lock)
          + " s; last server segment +" + ", +".join(f"{r['last_retx']:.1f}" for r in lock) + " s")
    print(f"recovery: {len(rec)} runs, detection {det_lo:.2f} to {det_hi:.2f} s, {n_win} window "
          f"refusals, setupComplete " + ", ".join(f"{r['setup']:.2f}" for r in rec)
          + f" s, first audio {audio_lo:.2f} to {audio_hi:.2f} s")
    for r in rec:
        print(f"  {r['label']}: {r['said']!r}")

    fig.canvas.draw()
    boxes = [(name, t.get_window_extent(renderer)) for name, t in labels]
    for i, (na, a) in enumerate(boxes):
        if a.x0 < 0 or a.x1 > W or a.y0 < 0 or a.y1 > H:
            problems.append(f"label runs off the figure: {na!r}")
        for nb, b in boxes[i + 1:]:
            if a.overlaps(b):
                problems.append(f"labels overlap: {na!r} / {nb!r}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=100, facecolor=SURFACE)
    print(f"wrote {OUT.relative_to(ROOT)}")
    for p in problems:
        print("problem:", p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
