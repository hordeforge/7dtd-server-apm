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
import json
import re
from bisect import bisect_left
from datetime import datetime
from pathlib import Path
from typing import Any

RE_SPIKE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}).*\[EfficientServer\]\s+SPIKE\s+"
    r"(?P<utc>\S+)\s+frame=(?P<frame>[\d.]+)ms.*zed=(?P<zed>-?\d+).*\|\s+(?P<top>.*)"
)


def parse_ts(s: str) -> float:
    # Server log stamps are host-local wall time with no offset field (the
    # dedicated server logs its local DateTime.Now); proc.jsonl "t" values are
    # true epoch seconds from time.time(). A naive .timestamp() applies this
    # host's zone rules including DST, keeping both sides on one clock;
    # stamping the naive value as UTC would shift every match by the UTC
    # offset and miss the window on any non-UTC host.
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").timestamp()  # noqa: DTZ007 -- naive on purpose, see rationale above
    except ValueError:
        return 0.0


def load_proc(capture: Path) -> list[dict[str, Any]]:
    p = capture / "memory" / "proc.jsonl"
    if not p.exists():
        p = capture / "proc.jsonl"
    with p.open("r", encoding="utf-8", errors="replace") as stream:
        return [json.loads(line) for line in stream if line.strip()]


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", type=Path, required=True)
    ap.add_argument("--game-log", type=Path, required=True)
    ap.add_argument("--window", type=float, default=2.0, help="seconds match window")
    args = ap.parse_args()

    proc = load_proc(args.capture)
    proc.sort(key=lambda r: r["t"])
    proc_times = [r["t"] for r in proc]
    text = args.game_log.read_text(encoding="utf-8", errors="replace")
    spikes: list[dict[str, Any]] = []
    for m in RE_SPIKE.finditer(text):
        spikes.append(
            {
                "ts": parse_ts(m.group("ts")),
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
                f"{sp['zed']:5d} {sp['top'][:70]}"
            )
        else:
            print(f"{sp['frame_ms']:8.1f} {'?':>7} {'?':>7} {sp['zed']:5d} {sp['top'][:70]}")

    # highlight host max cpu near any spike
    if proc:
        print("\nhost samples within 5s of any spike with cpu%>150% (multi-core):")
        spike_ts = sorted(s["ts"] for s in spikes)
        for r in proc:
            if r["cpu_pct"] < 150:
                continue
            # Any spike in [t-5, t+5]: binary search the first candidate at or
            # after t-5 and compare it against t+5, instead of scanning every
            # spike per sample.
            index = bisect_left(spike_ts, r["t"] - 5)
            if index < len(spike_ts) and spike_ts[index] < r["t"] + 5:
                print(
                    f"  t={r['t']:.0f} cpu={r['cpu_pct']:.0f}% rss={r['rss_mb']:.0f} thr={r['num_threads']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
