"""Resolve raw JIT addresses in bpftrace output against the bridge perf map.

bpftrace cannot read perf's /tmp/perf-<pid>.map, so managed frames in probe
output (e.g. mono_alloc allocation sites) stay as bare hex. This loads the map
the bridge exported (copied into the session) and rewrites `0xADDR` tokens to
their managed symbol, making allocation/stall sites human-readable.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from pathlib import Path

HEX = re.compile(r"\b0x([0-9a-fA-F]{6,})\b")


def load_map(map_path: Path) -> tuple[list[int], list[tuple[int, str]]]:
    starts: list[int] = []
    entries: list[tuple[int, str]] = []  # (end, symbol)
    for line in map_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        try:
            start = int(parts[0], 16)
            size = int(parts[1], 16)
        except ValueError:
            continue
        starts.append(start)
        entries.append((start + size, parts[2]))
    order = sorted(range(len(starts)), key=lambda i: starts[i])
    return [starts[i] for i in order], [entries[i] for i in order]


def annotate(text: str, starts: list[int], entries: list[tuple[int, str]]) -> str:
    if not starts:
        return text

    def resolve(match: re.Match[str]) -> str:
        addr = int(match.group(1), 16)
        index = bisect_right(starts, addr) - 1
        if 0 <= index < len(entries):
            end, symbol = entries[index]
            if addr < end:
                return f"{symbol}+0x{addr - starts[index]:x}"
        return match.group(0)

    return HEX.sub(resolve, text)


def annotate_session(session: Path) -> int:
    """Annotate every *.bt.out that has hex JIT frames. Returns files touched."""
    # Deterministic map pick: the cpu/perf map wins (capture-time canonical),
    # then any others by path. Directory read order must not decide which JIT
    # symbol table annotates evidence that later feeds reports.
    cpu_maps = sorted((session / "cpu/perf").glob("perf-*.map"))
    other_maps = sorted(p for p in session.glob("**/perf-*.map") if p not in cpu_maps)
    maps = cpu_maps + other_maps
    if not maps:
        return 0
    starts, entries = load_map(maps[0])
    if not starts:
        return 0
    touched = 0
    for out in session.glob("**/*.bt.out"):
        text = out.read_text(encoding="utf-8", errors="replace")
        if "0x" not in text:
            continue
        annotated = annotate(text, starts, entries)
        if annotated != text:
            out.with_suffix(".annotated.txt").write_text(annotated, encoding="utf-8")
            touched += 1
    return touched
