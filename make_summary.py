"""Rebuild results/summary.md from results/*.jsonl, with no manual step.

Each run of resume_test.py starts with a `run_start` event (its configuration) and ends
with a `run_end` event (the summary row the harness computed live). This script groups
consecutive runs of one file that share a configuration, orders the groups by start
time, and formats them with resume_test.py's own code (summary_columns, summary_header,
summary_block), so every number in summary.md comes from the JSONL.

One column is checked instead of only copied: `gap` in the BR1/BR2 tables. The harness
that ran BR2 run 1 took its "last heard" snapshot at the blackhole; an audio chunk
already in the client kernel was read 7 ms later, so that run's stored gap was too long.
The harness was fixed before BR2 run 2 (snapshot at detection). Here the gap is
recomputed for every BR run from the audio_segment events with the playback model of
resume_test.py (check_gaps); a stored value is replaced only when it is provably stale,
and each block says what was compared and what was replaced.

Rows from an earlier harness version can lack a column that was added later. A column
missing from every row of a block is dropped, or replaced by the columns that version
had in its place (LEGACY_COLUMNS).

Run: uv run make_summary.py    (offline, no key)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from resume_test import OUT_AUDIO_RATE, summary_block, summary_columns, summary_header

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
LEGACY_COLUMNS = {"resume_while_frozen": ("resume_attempts", "resume_from_freeze")}  # N1 run 1


def load(path: Path) -> dict[int, list[dict]]:
    runs: dict[int, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            runs.setdefault(o["run"], []).append(o)
    return runs


def recompute_gap(events: list[dict]) -> dict | None:
    """Gap from the last audio heard before the loss to the first model audio of the
    recovered session, as resume_test.summarize_recovery computes it, from the events.
    Model audio received on connection 1 plays back to back from its arrival
    (`interrupted` flushes it). Per-chunk arrival times are not logged, so each
    audio_segment plays from its first chunk for its total duration. That can only put
    the end of playback too early (never too late): it is exact unless a chunk arrived
    after the previous one had finished playing."""
    first = {}
    for e in events:
        first.setdefault(e["event"], e)
    if not {"blackhole_start", "link_lost_detected", "restore_sent"} <= set(first):
        return None
    t_bh, t_det = first["blackhole_start"]["t_ms"], first["link_lost_detected"]["t_ms"]
    old = [e for e in events if e["event"] == "audio_segment" and e["conn"] == 1]
    marks = [(e["first_ms"], "audio", e) for e in old]
    marks += [(e["t_ms"], "interrupted", e) for e in events
              if e["event"] == "interrupted" and e.get("conn") == 1 and e["t_ms"] <= t_det]
    play = -1.0
    for t, kind, e in sorted(marks, key=lambda m: m[0]):
        play = (max(play, t) + e["bytes"] / (OUT_AUDIO_RATE * 2) * 1000 if kind == "audio"
                else min(play, t))
    play_ms = round(play) if play >= 0 else None
    user_end = max([e["t_ms"] for e in events if e["event"] == "user_audio_end"
                    and e["t_ms"] <= t_bh], default=None)
    heard = max([v for v in (play_ms, user_end) if v is not None], default=None)
    new = [e["first_ms"] for e in events if e["event"] == "audio_segment"
           and e["conn"] != 1 and e["first_ms"] >= first["restore_sent"]["t_ms"]]
    late = [e["last_ms"] for e in old if e["last_ms"] > t_bh]
    return {"t_bh": t_bh, "heard": heard, "first_new": min(new) if new else None,
            "which": ("model audio playback" if heard is not None and heard == play_ms
                      else "end of the user's utterance"),
            "late_audio_ms": max(late) if late else None}


def format_gap(g: dict) -> str:
    if g["first_new"] is None or g["heard"] is None:
        return f"no model audio in the recovered session (last heard @{g['heard']})"
    return (f"{(g['first_new'] - g['heard']) / 1000:.2f} s (last heard @{g['heard']}: "
            f"{g['which']}; first new audio @{g['first_new']}; "
            f"+{(g['first_new'] - g['t_bh']) / 1000:.2f} s after the blackhole)")


def check_gaps(runs: list[tuple[int, list[dict], dict]]) -> str:
    """Compare each stored gap with the recomputed one. A stored value is replaced only
    when it is provably stale: model audio on connection 1 arrived after the blackhole
    and the stored "last heard" is earlier than the reconstruction, which is itself a
    lower bound of the true end of playback."""
    same, notes = 0, []
    for run, events, row in runs:
        g = recompute_gap(events)
        m = re.search(r"last heard @(\d+)", str(row.get("gap", "")))
        if g is None or m is None:
            continue
        stored = int(m.group(1))
        if stored == g["heard"]:
            same += 1
        elif stored < g["heard"] and g["late_audio_ms"] is not None:
            notes.append(
                f"Run {run}: replaced; stored at run time: \"{row['gap']}\". Model audio on "
                f"connection 1 kept arriving after the blackhole (last chunk @{g['late_audio_ms']}, "
                f"blackhole @{g['t_bh']}), and the harness version of that run took its snapshot "
                f"of what had been heard at the blackhole instead of at detection (fixed before "
                f"the next run). The value shown is recomputed from the audio segments; since the "
                f"reconstruction can only put the end of playback too early, it is an upper bound "
                f"of the gap, exact if the chunks played back to back")
            row["gap"] = format_gap(g)
        else:
            notes.append(
                f"Run {run}: kept the value stored at run time (last heard @{stored}, computed "
                f"per chunk); the reconstruction from segments gives @{g['heard']} "
                f"({stored - g['heard']:+d} ms), because a chunk arrived after the previous one "
                f"had finished playing")
    total = same + len(notes)
    return (f"`gap` check (make_summary.py recomputes it from the audio_segment events): "
            f"{same} of {total} runs equal to the value stored at run time."
            + "".join(f" {n}." for n in notes))


def blocks() -> list[tuple[str, str]]:
    out = []
    for path in sorted(RESULTS.glob("*.jsonl")):
        name, groups, prev = path.stem, [], None
        for run, events in sorted(load(path).items()):
            start = next(e for e in events if e["event"] == "run_start")
            end = next(e for e in events if e["event"] == "run_end")
            cfg = {k: v for k, v in start.items() if k not in ("run", "t_ms", "event", "wall")}
            if cfg != prev:
                groups.append({"cfg": cfg, "wall": start["wall"], "runs": []})
                prev = cfg
            groups[-1]["runs"].append((run, events, dict(end["summary"])))
        for g in groups:
            out.append((g["wall"], render(name, g)))
    return [b for _, b in sorted(out)]


def render(name: str, g: dict) -> str:
    cfg, runs = g["cfg"], g["runs"]
    rows = [r for _, _, r in runs]
    cols = []
    for c in summary_columns(cfg["scenario"]):
        if any(c in r for r in rows):
            cols.append(c)
        else:
            cols += [x for x in LEGACY_COLUMNS.get(c, ()) if any(x in r for r in rows)]
    notes = [check_gaps(runs)] if "gap" in cols else []
    first, last = runs[0][0], runs[-1][0]
    title = f"{name}, run {first}" if first == last else f"{name}, runs {first} to {last}"
    when = "first run started " + g["wall"].replace("T", " ")[:16]
    block = summary_block(title, summary_header(cfg, when), cols, rows)
    return block + "".join(n + "\n\n" for n in notes)


def main() -> None:
    head = ("# Summary tables\n\nGenerated by `uv run make_summary.py` from the `run_start` and "
            "`run_end` events in `results/*.jsonl`; do not edit by hand. One block per group of "
            "consecutive runs with the same configuration, in start order. The hand-checked "
            "tables and their reading are in FINDINGS.md.\n\n")
    text = head + "".join(blocks())
    text = re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"
    (RESULTS / "summary.md").write_text(text, encoding="utf-8")
    print(f"wrote {RESULTS.relative_to(HERE) / 'summary.md'} ({text.count(chr(10))} lines)")


if __name__ == "__main__":
    main()
