#!/usr/bin/env python3
"""Correlate host capture (proc/eBPF) with game log spikes.

Usage:
  uv run python tools/host_profiler/correlate.py \
    --capture ~/.local/share/7dtd-server-apm/session_... \
    --game-log /path/to/server/output_log.txt

Prefers APM session dirs (memory/proc.jsonl; a root-level proc.jsonl from
older layouts still works). Legacy EfficientServer SPIKE lines still parse if present.
"""

from __future__ import annotations

import argparse
import re
import sys
from bisect import bisect_left
from datetime import datetime
from pathlib import Path
from typing import Any

# apm_suite is resolved from the repository checkout, not the interpreter's
# venv: these scripts run under the resolved project interpreter (shell
# callers go through scripts/lib/python.sh -> SEVENDTD_APM_PYTHON), which is not
# necessarily this interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apm_suite.io import force_utf8_stdio, iter_jsonl

RE_SPIKE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}).*\[EfficientServer\]\s+SPIKE\s+"
    r"(?P<utc>\S+)\s+frame=(?P<frame>[\d.]+)ms.*zed=(?P<zed>-?\d+).*\|\s+(?P<top>.*)"
)
# Cap on the printed top frame, in characters (str units on text already
# decoded from the game log, so the cut never lands inside a multi-byte
# sequence). It is a terminal column, but the column is last on the line and
# the names are symbol text, so a display-width bound is not the constraint.
TOP_CHARS = 70


def parse_ts(s: str) -> float:
    # Server log stamps are host-local wall time with no offset field (the
    # dedicated server logs its local DateTime.Now); proc.jsonl "t" values are
    # true epoch seconds from time.time(). A naive .timestamp() applies this
    # host's zone rules including DST, keeping both sides on one clock;
    # stamping the naive value as UTC would shift every match by the UTC
    # offset and miss the window on any non-UTC host.
    #
    # The wall clock is ambiguous twice a year and this is the only thing
    # standing between a spike and its host samples: on a fall-back date the
    # 02:30 stamp occurs twice, and both occurrences resolve to the FIRST
    # (DST) one, so every spike logged during the repeated hour is correlated
    # against samples an hour away. spike_epoch() prefers the line's own UTC
    # stamp for exactly that reason; this stays the fallback.
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").timestamp()  # noqa: DTZ007 -- naive on purpose, see rationale above
    except ValueError:
        return 0.0


def parse_utc_ts(s: str) -> float | None:
    """Epoch seconds for an ISO stamp that carries its own offset, else None.

    Only an explicit offset (or Z) counts. A stamp without one is a wall clock
    reading, and accepting it here would hide the ambiguity this function
    exists to remove.
    """
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def spike_epoch(wall: str, utc: str) -> float:
    """The instant a SPIKE line records, from its UTC stamp when it has one."""
    stamped = parse_utc_ts(utc)
    return parse_ts(wall) if stamped is None else stamped


def proc_jsonl(capture: Path) -> Path:
    # A capture can lack the memory layer entirely; the caller reports the path
    # it looked for rather than letting iter_jsonl raise FileNotFoundError.
    modern = capture / "memory" / "proc.jsonl"
    return modern if modern.exists() else capture / "proc.jsonl"


def near_spike(spike_ts: list[float], t: float, window: float = 5.0) -> bool:
    """True when a spike stamp falls in [t - window, t + window).

    The window is anchored on the sample, half-open at the far end: a spike
    exactly `window` before the sample counts, one exactly `window` after it
    does not. `spike_ts` must be sorted ascending.
    """
    index = bisect_left(spike_ts, t - window)
    return index < len(spike_ts) and spike_ts[index] < t + window


def nearest_proc(times: list[float], rows: list[dict[str, Any]], t: float) -> dict[str, Any] | None:
    """Sample with the smallest |t - stamp|; ties prefer the EARLIER sample.

    `rows` must be sorted by their "t" ascending with the parallel `times` list
    holding those stamps, so each lookup is a binary search instead of a full
    scan (a long game log against a dense proc.jsonl is O(spikes x samples)
    otherwise).
    """
    if not rows:
        return None
    index = bisect_left(times, t)
    candidates: list[dict[str, Any]] = []
    if index > 0:
        candidates.append(rows[index - 1])
    if index < len(rows):
        candidates.append(rows[index])
    return min(candidates, key=lambda r: abs(r["t"] - t))


def main() -> int:
    force_utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", type=Path, required=True)
    ap.add_argument("--game-log", type=Path, required=True)
    ap.add_argument("--window", type=float, default=2.0, help="seconds match window")
    args = ap.parse_args()

    proc_path = proc_jsonl(args.capture)
    if not proc_path.is_file():
        print(
            f"no proc samples in {args.capture} (looked for memory/proc.jsonl, proc.jsonl); "
            "capture with the memory/threads layer or pass a session that has it",
            file=sys.stderr,
        )
        return 2
    try:
        proc = list(iter_jsonl(proc_path))
    except OSError as error:
        print(f"cannot read proc samples from {proc_path}: {error}", file=sys.stderr)
        return 2
    proc.sort(key=lambda r: r["t"])
    proc_times = [r["t"] for r in proc]
    text = args.game_log.read_text(encoding="utf-8", errors="replace")
    spikes: list[dict[str, Any]] = []
    for m in RE_SPIKE.finditer(text):
        spikes.append(
            {
                "ts": spike_epoch(m.group("ts"), m.group("utc")),
                "frame_ms": float(m.group("frame")),
                "zed": int(m.group("zed")),
                "top": m.group("top").strip(),
            }
        )

    print(f"capture={args.capture}")
    print(f"game-log spikes={len(spikes)} proc_samples={len(proc)}")
    if not spikes:
        print("no SPIKE lines in game log; try lower LogSpikeMs or generate load")
        if proc:
            cpus = [r["cpu_pct"] for r in proc[1:]]
            if cpus:
                print(f"host cpu% mean={sum(cpus) / len(cpus):.1f} max={max(cpus):.1f}")
        return 0

    print(f"{'frame_ms':>8} {'cpu%':>7} {'rssMB':>7} {'zed':>5} top")
    for sp in spikes:
        pr = nearest_proc(proc_times, proc, sp["ts"])
        if pr and abs(pr["t"] - sp["ts"]) <= args.window + 5:
            print(
                f"{sp['frame_ms']:8.1f} {pr['cpu_pct']:7.1f} {pr['rss_mb']:7.1f} "
                f"{sp['zed']:5d} {sp['top'][:TOP_CHARS]}"
            )
        else:
            print(f"{sp['frame_ms']:8.1f} {'?':>7} {'?':>7} {sp['zed']:5d} {sp['top'][:TOP_CHARS]}")

    if proc:
        print("\nhost samples within 5s of any spike with cpu%>150% (multi-core):")
        spike_ts = sorted(s["ts"] for s in spikes)
        for r in proc:
            if r["cpu_pct"] < 150:
                continue
            # Any spike in [t-5, t+5): binary search the first candidate at or
            # after t-5 and compare it against t+5, instead of scanning every
            # spike per sample.
            if near_spike(spike_ts, r["t"]):
                print(
                    f"  t={r['t']:.0f} cpu={r['cpu_pct']:.0f}% rss={r['rss_mb']:.0f} thr={r['num_threads']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
