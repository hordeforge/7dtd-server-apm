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


def read_stat(pid: int) -> dict:
    # man proc_pid_stat
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()
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
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("voluntary_ctxt_switches:"):
            vol = int(line.split()[1])
        elif line.startswith("nonvoluntary_ctxt_switches:"):
            non = int(line.split()[1])
    return vol, non


def read_io(pid: int) -> dict:
    out = {"rchar": 0, "wchar": 0, "read_bytes": 0, "write_bytes": 0}
    p = Path(f"/proc/{pid}/io")
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
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


def top_threads(pid: int, n: int = 8) -> list[dict]:
    tasks = Path(f"/proc/{pid}/task")
    rows = []
    try:
        for tid in tasks.iterdir():
            try:
                st = read_stat(int(tid.name))
                comm = Path(f"/proc/{pid}/task/{tid.name}/comm").read_text().strip()
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
                    [str(root / "tools/host_profiler/find_server.sh")], text=True
                ).strip()
            )
        except Exception as e:
            print(f"need --pid or running server: {e}")
            return 1

    end = time.time() + args.seconds
    prev = None
    rows = []
    print(f"sampling pid={pid} for {args.seconds}s every {args.interval}s")
    print(f"{'t':>8} {'cpu%':>7} {'rssMB':>8} thr  fd  {'rMB/s':>7} {'wMB/s':>7} vctx  nvctx")
    t0 = time.time()
    while time.time() < end:
        if not Path(f"/proc/{pid}").exists():
            print("process exited")
            break
        t_before = time.time()
        # /proc reads race with process exit (the exists() check above is TOCTOU)
        # and can return short/empty files; a bad sample must not kill the run.
        try:
            s = sample(pid)
        except (OSError, ValueError, IndexError):
            print("process exited or /proc read raced")
            break
        # fix cpu for first sample interval after sleep
        if prev is not None:
            dt = s.t - prev.t
            user_hz = os.sysconf("SC_CLK_TCK")
            d_ticks = (s.utime + s.stime) - (prev.utime + prev.stime)
            s.cpu_pct = 100.0 * (d_ticks / user_hz) / max(dt, 1e-3)
            dr = (s.read_bytes - prev.read_bytes) / max(dt, 1e-3) / (1024 * 1024)
            dw = (s.write_bytes - prev.write_bytes) / max(dt, 1e-3) / (1024 * 1024)
            dvc = s.voluntary_ctx - prev.voluntary_ctx
            dnc = s.nonvoluntary_ctx - prev.nonvoluntary_ctx
        else:
            dr = dw = 0.0
            dvc = dnc = 0
        rel = s.t - t0
        print(
            f"{rel:8.1f} {s.cpu_pct:7.1f} {s.rss_mb:8.1f} {s.num_threads:3d} {s.fd_count:4d} "
            f"{dr:7.2f} {dw:7.2f} {dvc:5d} {dnc:5d}"
        )
        if args.threads:
            # One walk of /proc/pid/task per sample feeds both the console line
            # and the JSONL record; a second call would re-read stat+comm for
            # every thread (100+ files/s wasted on a loaded server).
            thr = top_threads(pid)
            for t in thr[:5]:
                print(f"    tid={t['tid']:<7} {t['comm']:<16} ticks={t['cpu_ticks']}")
        rec = asdict(s)
        if args.threads:
            rec["top_threads"] = thr
        rows.append(rec)
        prev = s
        # sleep remaining
        elapsed = time.time() - t_before
        time.sleep(max(0.0, args.interval - elapsed))

    if args.json and rows:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        with args.json.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"wrote {args.json}")

        # summary
        cpus = [r["cpu_pct"] for r in rows[1:]]
        if cpus:
            print(
                f"summary cpu% mean={sum(cpus) / len(cpus):.1f} max={max(cpus):.1f} "
                f"rssMB last={rows[-1]['rss_mb']:.1f} threads={rows[-1]['num_threads']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
