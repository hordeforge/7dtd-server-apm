#!/usr/bin/env python3
"""Rootless /proc sampler for 7DTD dedicated server.

Samples CPU, memory, threads, fds, IO, context switches, and top threads.
No eBPF required.

Usage:
  python3 tools/host_profiler/proc_sample.py --pid $(tools/host_profiler/find_server.sh) --seconds 60
  python3 tools/host_profiler/proc_sample.py --seconds 30 --json out/proc.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any


@dataclass
class Sample:
    t: float
    pid: int
    utime: int
    stime: int
    cutime: int
    cstime: int
    num_threads: int
    vsize: int
    rss_pages: int
    voluntary_ctx: int
    nonvoluntary_ctx: int
    fd_count: int
    read_bytes: int
    write_bytes: int
    rchar: int
    wchar: int
    cpu_pct: float
    rss_mb: float
    # Monotonic companion to the wall stamp `t`: rate math (rss/fd growth per
    # second) must divide by a clock that cannot step (NTP correction, manual
    # change); `t` stays for cross-log correlation.
    mono: float


def read_stat(pid: int) -> dict[str, int]:
    # man proc_pid_stat
    fields = (
        Path(f"/proc/{pid}/stat")
        .read_text(encoding="utf-8", errors="replace")
        .rsplit(")", 1)[-1]
        .split()
    )
    # after comm: state=0, ppid=1, ... utime=11, stime=12, cutime=13, cstime=14, num_threads=17, vsize=20, rss=21
    return {
        "utime": int(fields[11]),
        "stime": int(fields[12]),
        "cutime": int(fields[13]),
        "cstime": int(fields[14]),
        "num_threads": int(fields[17]),
        "vsize": int(fields[20]),
        "rss_pages": int(fields[21]),
    }


def read_status_ctx(pid: int) -> tuple[int, int]:
    vol = non = 0
    for line in (
        Path(f"/proc/{pid}/status").read_text(encoding="ascii", errors="replace").splitlines()
    ):
        if line.startswith("voluntary_ctxt_switches:"):
            vol = int(line.split()[1])
        elif line.startswith("nonvoluntary_ctxt_switches:"):
            non = int(line.split()[1])
    return vol, non


def read_io(pid: int) -> dict[str, int]:
    out = {"rchar": 0, "wchar": 0, "read_bytes": 0, "write_bytes": 0}
    p = Path(f"/proc/{pid}/io")
    if not p.exists():
        return out
    for line in p.read_text(encoding="ascii", errors="replace").splitlines():
        k, _, v = line.partition(":")
        k = k.strip()
        if k in out:
            out[k] = int(v.strip())
    return out


def fd_count(pid: int) -> int:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return -1


def top_threads(pid: int, n: int = 8) -> list[dict[str, Any]]:
    tasks = Path(f"/proc/{pid}/task")
    rows: list[dict[str, Any]] = []
    try:
        for tid in tasks.iterdir():
            try:
                st = read_stat(int(tid.name))
                comm = (
                    Path(f"/proc/{pid}/task/{tid.name}/comm")
                    .read_text(encoding="utf-8", errors="replace")
                    .strip()
                )
                rows.append(
                    {
                        "tid": int(tid.name),
                        "comm": comm,
                        "utime": st["utime"],
                        "stime": st["stime"],
                        "cpu_ticks": st["utime"] + st["stime"],
                    }
                )
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return []
    rows.sort(key=lambda r: r["cpu_ticks"], reverse=True)
    return rows[:n]


def sample(pid: int) -> Sample:
    st = read_stat(pid)
    vol, non = read_status_ctx(pid)
    io = read_io(pid)
    page = os.sysconf("SC_PAGE_SIZE")
    # cpu_pct is derived in the main loop from the real elapsed dt between
    # samples (the interval here is only the target); first sample stays 0.
    cpu_pct = 0.0
    return Sample(
        t=time.time(),
        mono=time.monotonic(),
        pid=pid,
        utime=st["utime"],
        stime=st["stime"],
        cutime=st["cutime"],
        cstime=st["cstime"],
        num_threads=st["num_threads"],
        vsize=st["vsize"],
        rss_pages=st["rss_pages"],
        voluntary_ctx=vol,
        nonvoluntary_ctx=non,
        fd_count=fd_count(pid),
        read_bytes=io["read_bytes"],
        write_bytes=io["write_bytes"],
        rchar=io["rchar"],
        wchar=io["wchar"],
        cpu_pct=cpu_pct,
        rss_mb=st["rss_pages"] * page / (1024 * 1024),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--threads", action="store_true", help="print top threads each sample")
    args = ap.parse_args()
    pid = args.pid
    if not pid:
        import subprocess

        try:
            root = Path(__file__).resolve().parents[2]
            pid = int(
                subprocess.check_output(
                    [str(root / "tools/host_profiler/find_server.sh")],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                ).strip()
            )
        except Exception as e:
            print(f"need --pid or running server: {e}")
            return 1

    # Loop control and rate math run on the monotonic clock: a wall-clock step
    # (NTP correction, manual change) mid-run would otherwise end the window
    # early/late or divide rates by a near-zero/negative dt. The persisted
    # record stamp `t` stays wall-clock so samples correlate with logs.
    end = time.monotonic() + args.seconds
    start_mono = time.monotonic()
    # Stream each record as it is sampled instead of buffering until exit: the
    # capture supervisor SIGTERMs this collector at the window deadline, and a
    # buffered epilogue never runs then, silently discarding the whole run's
    # evidence. Readers tolerate a torn final line from a mid-write kill.
    out_fh = None
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        out_fh = args.json.open("w", encoding="utf-8")
    try:
        _sample_loop(args, pid, end, start_mono, out_fh)
    finally:
        if out_fh is not None:
            out_fh.close()
            print(f"wrote {args.json}")
    return 0


def _sample_loop(
    args: argparse.Namespace,
    pid: int,
    end: float,
    start_mono: float,
    out_fh: IO[str] | None,
) -> None:
    prev: Sample | None = None
    prev_mono: float | None = None
    # Streaming aggregates for the end-of-run summary (first sample has no dt).
    cpu_sum = 0.0
    cpu_samples = 0.0
    cpu_max = 0.0
    last: Sample | None = None
    print(f"sampling pid={pid} for {args.seconds}s every {args.interval}s")
    print(f"{'t':>8} {'cpu%':>7} {'rssMB':>8} thr  fd  {'rMB/s':>7} {'wMB/s':>7} vctx  nvctx")
    while time.monotonic() < end:
        if not Path(f"/proc/{pid}").exists():
            print("process exited")
            break
        mono_before = time.monotonic()
        # /proc reads race with process exit (the exists() check above is TOCTOU)
        # and can return short/empty files; a bad sample must not kill the run.
        try:
            s = sample(pid)
        except (OSError, ValueError, IndexError):
            print("process exited or /proc read raced")
            break
        mono_after = time.monotonic()
        # fix cpu for first sample interval after sleep
        if prev is not None and prev_mono is not None:
            dt = max(mono_after - prev_mono, 1e-3)
            user_hz = os.sysconf("SC_CLK_TCK")
            d_ticks = (s.utime + s.stime) - (prev.utime + prev.stime)
            s.cpu_pct = 100.0 * (d_ticks / user_hz) / dt
            dr = (s.read_bytes - prev.read_bytes) / dt / (1024 * 1024)
            dw = (s.write_bytes - prev.write_bytes) / dt / (1024 * 1024)
            dvc = s.voluntary_ctx - prev.voluntary_ctx
            dnc = s.nonvoluntary_ctx - prev.nonvoluntary_ctx
        else:
            dr = dw = 0.0
            dvc = dnc = 0
        rel = mono_after - start_mono
        print(
            f"{rel:8.1f} {s.cpu_pct:7.1f} {s.rss_mb:8.1f} {s.num_threads:3d} {s.fd_count:4d} "
            f"{dr:7.2f} {dw:7.2f} {dvc:5d} {dnc:5d}"
        )
        rec = asdict(s)
        if args.threads:
            # One walk of /proc/pid/task per sample feeds both the console line
            # and the JSONL record; a second call would re-read stat+comm for
            # every thread (100+ files/s wasted on a loaded server).
            thr = top_threads(pid)
            for t in thr[:5]:
                print(f"    tid={t['tid']:<7} {t['comm']:<16} ticks={t['cpu_ticks']}")
            rec["top_threads"] = thr
        if out_fh is not None:
            out_fh.write(json.dumps(rec) + "\n")
            out_fh.flush()
        if prev is not None:
            cpu_samples += 1.0
            cpu_sum += s.cpu_pct
            cpu_max = max(cpu_max, s.cpu_pct)
        last = s
        prev = s
        prev_mono = mono_after
        # sleep remaining
        time.sleep(max(0.0, args.interval - (time.monotonic() - mono_before)))

    if last is not None:
        mean = cpu_sum / cpu_samples if cpu_samples else 0.0
        print(
            f"summary cpu% mean={mean:.1f} max={cpu_max:.1f} "
            f"rssMB last={last.rss_mb:.1f} threads={last.num_threads}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
