#!/usr/bin/env python3
"""Render the cold-open cut of the v2 demo clip, for feeds where the first seconds decide
whether people watch (rule of 2026-10-03: open on the payoff, labelled as an excerpt, then
the full run). Nothing is re-recorded or re-timed: every frame and sample comes from
results/clip/lockout-recovery-v2.mp4, rendered by make_clip.py.

  3.0 s   excerpt, without recovery: the end still of the first half (v2 24.5 s to 27.5 s),
          "After 15 minutes in our tests: still refused, the user is never told."
  5.7 s   excerpt, with recovery: "Did you book it?" and the model's answer
          (v2 43.6 s to 49.3 s, real time, with its audio)
  52.5 s  the full v2 clip, from its title card

Each excerpt carries an "Excerpt" label in the bottom-right corner.

Output: results/clip/lockout-recovery-v2-coldopen.mp4 (1280x720, 30 fps, H.264 + AAC mono
24 kHz, faststart).

Run: uv run --with imageio-ffmpeg python make_coldopen.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "results" / "clip" / "lockout-recovery-v2.mp4"
OUT = ROOT / "results" / "clip" / "lockout-recovery-v2-coldopen.mp4"
FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
EXCERPTS = [  # (start s, end s, label) in the v2 clip
    (24.5, 27.5, "Excerpt: without recovery. Full run follows."),
    (43.6, 49.3, "Excerpt: with recovery. Full run follows."),
]


def main() -> None:
    font = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
    if font is None:
        raise SystemExit("no font found for the excerpt label")
    parts, inputs = [], []
    for i, (a, b, label) in enumerate(EXCERPTS):
        text = label.replace(":", r"\:").replace(",", r"\,")
        parts.append(
            f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS,"
            f"drawtext=fontfile='{font}':text='{text}':fontsize=22:fontcolor=white:"
            f"box=1:boxcolor=0x1a1a19@0.85:boxborderw=12:x=w-tw-28:y=h-th-28[v{i}];"
            f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}];"
        )
        inputs.append(f"[v{i}][a{i}]")
    n = len(EXCERPTS)
    parts.append(f"[0:v]setpts=PTS-STARTPTS[v{n}];[0:a]asetpts=PTS-STARTPTS[a{n}];")
    inputs.append(f"[v{n}][a{n}]")
    graph = "".join(parts) + "".join(inputs) + f"concat=n={n + 1}:v=1:a=1[v][a]"
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(SRC),
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-crf", "20",
        "-c:a", "aac", "-ar", "24000", "-ac", "1", "-b:a", "64k",
        "-movflags", "+faststart", str(OUT),
    ]
    subprocess.run(cmd, check=True)
    print(OUT)


if __name__ == "__main__":
    main()
