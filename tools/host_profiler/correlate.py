#!/usr/bin/env python3
"""Correlate host capture (proc/eBPF) with game log spikes.

Usage:
  uv run python tools/host_profiler/correlate.py \
    --capture ~/.local/share/7dtd-apm/session_... \
    --game-log /path/to/server/output_log.txt

Prefers APM session dirs (proc.jsonl). Legacy EfficientServer SPIKE lines still parse if present.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

# Reuse game log spike parser patterns
RE_SPIKE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}).*\[EfficientServer\]\s+SPIKE\s+"
    r"(?P<utc>\S+)\s+frame=(?P<frame>[\d.]+)ms.*zed=(?P<zed>-?\d+).*\|\s+(?P<top>.*)"
)
RE_PERF = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}).*\[EfficientServer\]\s+perf frame avg=(?P<avg>[\d.]+)ms"
)


def parse_ts(s: str) -> float:
    # local log timestamps without Z
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        return dt.timestamp()
    except ValueError:
        return 0.0


def load_proc(capture: Path) -> list[dict]:
    p = capture / "proc.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def nearest_proc(rows: list[dict], t: float) -> dict | None:
    if not rows:
        return None
    return min(rows, key=lambda r: abs(r["t"] - t))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", type=Path, required=True)
    ap.add_argument("--game-log", type=Path, required=True)
    ap.add_argument("--window", type=float, default=2.0, help="seconds match window")
    args = ap.parse_args()

    proc = load_proc(args.capture)
    text = args.game_log.read_text(errors="replace")
    spikes = []
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
        pr = nearest_proc(proc, sp["ts"])
        if pr and abs(pr["t"] - sp["ts"]) <= args.window + 5:
            print(
                f"{sp['frame_ms']:8.1f} {pr['cpu_pct']:7.1f} {pr['rss_mb']:7.1f} "
                f"{sp['zed']:5d} {sp['top'][:70]}"
            )
        else:
            print(f"{sp['frame_ms']:8.1f} {'?':>7} {'?':>7} {sp['zed']:5d} {sp['top'][:70]}")

    # highlight host max cpu near any spike
    if proc and spikes:
        print("\nhost samples within 5s of any spike with cpu%>150% (multi-core):")
        spike_ts = [s["ts"] for s in spikes]
        for r in proc:
            if r["cpu_pct"] < 150:
                continue
            if any(abs(r["t"] - st) < 5 for st in spike_ts):
                print(
                    f"  t={r['t']:.0f} cpu={r['cpu_pct']:.0f}% rss={r['rss_mb']:.0f} thr={r['num_threads']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
