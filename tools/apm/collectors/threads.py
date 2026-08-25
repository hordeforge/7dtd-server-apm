#!/usr/bin/env python3
"""Thread / contention sample from /proc for a process.

Captures per-tid CPU ticks, state, voluntary/nonvoluntary ctx, and wchan
(kernel wait channel — often futex_wait_queue_me when blocked on locks).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import time
from pathlib import Path
from typing import TypedDict


class ThreadRow(TypedDict):
    """One /proc/<pid>/task/<tid> sample; see read_tid."""

    tid: int
    comm: str
    state: str
    utime: int
    stime: int
    cpu_ticks: int
    vol_ctx: int
    nonvol_ctx: int
    processor: int
    wchan: str


class ThreadDelta(ThreadRow):
    """ThreadRow plus the per-interval deltas emitted to jsonl/console."""

    d_ticks: int
    d_vol_ctx: int
    d_nonvol_ctx: int
    cpu_pct: float


def read_tid(pid: int, tid: int) -> ThreadRow | None:
    base = Path(f"/proc/{pid}/task/{tid}")
    try:
        stat = (base / "stat").read_text(encoding="utf-8", errors="replace")
        # comm is in parens and may contain spaces
        lparen = stat.find("(")
        rparen = stat.rfind(")")
        if lparen == -1 or rparen == -1 or rparen < lparen:
            return None
        comm = stat[lparen + 1 : rparen]
        fields = stat[rparen + 2 :].split()
        # state, ..., utime(11), stime(12), ... num_threads not per-tid
        state = fields[0]
        utime = int(fields[11])
        stime = int(fields[12])
        # processor field index 36 in man proc (0-based after state fields...):
        # after comm: 0 state, 1 ppid, ... 11 utime, 12 stime, 36 processor = fields[36]
        processor = int(fields[36]) if len(fields) > 36 else -1
    except (OSError, IndexError, ValueError):
        return None

    vol = non = 0
    try:
        for line in (base / "status").read_text(encoding="ascii", errors="replace").splitlines():
            if line.startswith("voluntary_ctxt_switches:"):
                vol = int(line.split()[1])
            elif line.startswith("nonvoluntary_ctxt_switches:"):
                non = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        # /proc raced with thread exit, or a truncated line; keep the zeros.
        pass

    wchan = ""
    with contextlib.suppress(OSError):
        wchan = (base / "wchan").read_text(encoding="utf-8", errors="replace").strip()

    return {
        "tid": tid,
        "comm": comm,
        "state": state,
        "utime": utime,
        "stime": stime,
        "cpu_ticks": utime + stime,
        "vol_ctx": vol,
        "nonvol_ctx": non,
        "processor": processor,
        "wchan": wchan,
    }


def snapshot(pid: int) -> list[ThreadRow]:
    tasks = Path(f"/proc/{pid}/task")
    rows: list[ThreadRow] = []
    try:
        for t in tasks.iterdir():
            if not t.name.isdigit():
                continue
            r = read_tid(pid, int(t.name))
            if r:
                rows.append(r)
    except OSError:
        return []
    rows.sort(key=lambda r: r["cpu_ticks"], reverse=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--jsonl", type=Path, required=True)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    args.jsonl.parent.mkdir(parents=True, exist_ok=True)
    # Loop control and the cpu% dt run on the monotonic clock so a wall-clock
    # step (NTP correction) cannot end the window early/late or divide rates
    # by a near-zero/negative dt; the persisted record stamp "t" stays
    # wall-clock for cross-log correlation.
    end = time.monotonic() + args.seconds
    prev: dict[int, ThreadRow] = {}
    # Monotonic stamps for the previous sample, keyed like prev (the stamp is
    # loop control data, not part of the persisted thread record).
    prev_mono: dict[int, float] = {}
    user_hz = os.sysconf("SC_CLK_TCK")

    with args.jsonl.open("w", encoding="utf-8") as fh:
        while time.monotonic() < end:
            if not Path(f"/proc/{args.pid}").exists():
                break
            t0 = time.time()
            m0 = time.monotonic()
            rows = snapshot(args.pid)
            deltas: list[ThreadDelta] = []
            for r in rows:
                p = prev.get(r["tid"])
                m_prev = prev_mono.get(r["tid"])
                d_ticks = r["cpu_ticks"] - p["cpu_ticks"] if p else 0
                d_vol = r["vol_ctx"] - p["vol_ctx"] if p else 0
                d_non = r["nonvol_ctx"] - p["nonvol_ctx"] if p else 0
                dt = max(m0 - m_prev, 1e-3) if m_prev is not None else args.interval
                cpu_pct = 100.0 * (d_ticks / user_hz) / dt if p else 0.0
                delta: ThreadDelta = {
                    **r,
                    "d_ticks": d_ticks,
                    "d_vol_ctx": d_vol,
                    "d_nonvol_ctx": d_non,
                    "cpu_pct": round(cpu_pct, 2),
                }
                deltas.append(delta)
            deltas.sort(key=lambda x: x["cpu_pct"], reverse=True)
            # wchan histogram
            wchan: dict[str, int] = {}
            states: dict[str, int] = {}
            for r in rows:
                wchan[r["wchan"] or "-"] = wchan.get(r["wchan"] or "-", 0) + 1
                states[r["state"]] = states.get(r["state"], 0) + 1

            wchan_top = dict(sorted(wchan.items(), key=lambda kv: -kv[1])[:20])
            rec: dict[str, object] = {
                "t": t0,
                "n_threads": len(rows),
                "states": states,
                "wchan_top": wchan_top,
                "top": deltas[: args.top],
                # Whole-process CPU% over every sampled thread this pass. "top"
                # is truncated to --top rows, so readers wanting the main
                # thread's share OF THE PROCESS must divide by this, not by
                # the top-row sum (which would inflate the share whenever
                # more threads than the cap carry CPU).
                "process_cpu_pct": round(sum(d["cpu_pct"] for d in deltas), 2),
            }
            fh.write(json.dumps(rec) + "\n")
            fh.flush()

            # console line
            top3 = ", ".join(f"{x['comm'][:16]}@{x['cpu_pct']:.0f}%" for x in deltas[:3])
            blocked = states.get("D", 0) + states.get("S", 0)
            print(
                f"thr={len(rows)} S/D~={blocked} wchan={list(wchan_top.items())[:3]} top=[{top3}]"
            )

            prev = {r["tid"]: r for r in rows}
            prev_mono = {r["tid"]: m0 for r in rows}
            time.sleep(max(0.0, args.interval - (time.monotonic() - m0)))

    print(f"wrote {args.jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
