#!/usr/bin/env python3
"""Annotate folded stacks with C# system tags for Speedscope readability.

Rewrites frame names like:
  mono_gc_collect  →  [GC] mono_gc_collect
  Job.Worker       →  [JOBS] Job.Worker

so interactive flames and Speedscope show *which layer* a native frame belongs to,
bridging OS stacks toward mod work without full JIT symbolication.

Usage:
  python3 tools/host_profiler/annotate_stacks.py stacks.folded -o stacks.annotated.folded
  # then make_flames on the annotated file
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

# Order matters: first match wins (more specific first).
TAG_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("GC", re.compile(r"mono_gc|GC_gcollect|GC_try_to_collect|GC_dirty|libmonobdwgc|sgen_", re.I)),
    (
        "LOCK",
        re.compile(
            r"futex|pthread_mutex|pthread_cond|mono_monitor|mono_locks|Monitor\.|lock_internal",
            re.I,
        ),
    ),
    (
        "IO",
        re.compile(
            r"vfs_|ext4_|xfs_|nvme_|blk_|pread|pwrite|__x64_sys_read|__x64_sys_write|fsync", re.I
        ),
    ),
    ("PATH", re.compile(r"PathFinder|PathNavigate|Astar|ASPPath|pathFollow|pathfind", re.I)),
    ("AI", re.compile(r"EAIManager|EAIBase|updateTasks|OnUpdateLive|AIDirector|EntityAlive", re.I)),
    ("MESH", re.compile(r"DynamicMesh|ChunkMesh|mesh_build|Mesh", re.I)),
    ("NET", re.compile(r"LiteNetLib|ConnectionManager|tcp_send|tcp_recv|NetPackage|Socket", re.I)),
    ("JOBS", re.compile(r"Job\.Worker|JobQueue|Background Job|Worker Thread", re.I)),
    ("MAIN", re.compile(r"gmUpdate|GameManager|PlayerLoop|7DaysToDieServe|UnityMain", re.I)),
]


_TAG_CACHE: dict[str, str] = {}


def tag_frame(name: str) -> str:
    if name.startswith("["):
        return name  # already tagged
    # Frame names repeat across thousands of folded lines; run each name's rule
    # regexes once instead of once per occurrence.
    cached = _TAG_CACHE.get(name)
    if cached is not None:
        return cached
    for tag, pat in TAG_RULES:
        if pat.search(name):
            result = f"[{tag}] {name}"
            break
    else:
        result = name
    _TAG_CACHE[name] = result
    return result


def annotate_folded_line(line: str) -> str:
    line = line.strip()
    if not line:
        return line
    try:
        stack_s, count_s = line.rsplit(" ", 1)
        int(float(count_s))
    except (ValueError, OverflowError):
        # Malformed or non-finite count (int(inf) raises OverflowError): leave
        # the line untouched rather than crash the annotation pass.
        return line
    frames = [tag_frame(f) for f in stack_s.split(";") if f]
    return " ".join([";".join(frames), count_s])


def annotate_file(src: Path, dst: Path) -> dict[str, Any]:
    tagged = 0
    total_frames = 0
    stacks = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Streamed line by line: folded inputs reach hundreds of MB, and the former
    # read-all/transform/join/write held roughly three resident copies of the
    # file for nothing. Each emitted line ends with "\n" exactly like the
    # former splitlines-join, so the bytes are unchanged.
    with (
        src.open("r", encoding="utf-8", errors="replace") as reader,
        dst.open("w", encoding="utf-8") as writer,
    ):
        for line in reader:
            ann = annotate_folded_line(line)
            if not ann:
                continue
            stacks += 1
            writer.write(ann + "\n")
            stack = ann.rsplit(" ", 1)[0]
            for fr in stack.split(";"):
                total_frames += 1
                if fr.startswith("["):
                    tagged += 1
    return {
        "stacks": stacks,
        "frames": total_frames,
        "tagged_frames": tagged,
        "output": str(dst),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folded", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    args = ap.parse_args()
    if not args.folded.is_file():
        print(f"missing {args.folded}", file=sys.stderr)
        return 1
    stats = annotate_file(args.folded, args.output)
    print(
        f"annotated stacks={stats['stacks']} frames={stats['frames']} "
        f"tagged={stats['tagged_frames']} -> {stats['output']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
