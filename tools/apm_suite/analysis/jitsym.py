"""Resolve raw JIT addresses in bpftrace output against the bridge perf map.

bpftrace cannot read perf's /tmp/perf-<pid>.map, so managed frames in probe
output (e.g. mono_alloc allocation sites) stay as bare hex. This loads the map
the bridge exported (copied into the session) and rewrites `0xADDR` tokens to
their managed symbol, making allocation/stall sites human-readable.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable
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


def _resolver(starts: list[int], entries: list[tuple[int, str]]) -> Callable[[re.Match[str]], str]:
    def resolve(match: re.Match[str]) -> str:
        addr = int(match.group(1), 16)
        index = bisect_right(starts, addr) - 1
        if 0 <= index < len(entries):
            end, symbol = entries[index]
            if addr < end:
                return f"{symbol}+0x{addr - starts[index]:x}"
        return match.group(0)

    return resolve


def annotate(text: str, starts: list[int], entries: list[tuple[int, str]]) -> str:
    if not starts:
        return text
    return HEX.sub(_resolver(starts, entries), text)


def _annotate_stream(
    source: Path, target: Path, starts: list[int], entries: list[tuple[int, str]]
) -> bool:
    """Rewrite hex tokens line by line into `target`; True when anything changed.

    Hex address tokens never span lines, so per-line substitution reproduces
    the former whole-text annotate() byte for byte while keeping memory at one
    line plus the unchanged prefix, not the full text and its annotated twin.
    The target is opened only on the first changed line: a no-change pass must
    leave NO .annotated.txt behind, because readers prefer that file whenever
    it exists and an empty one would shadow the raw evidence.
    """
    resolve = _resolver(starts, entries)
    pending: list[str] = []
    dst = None
    try:
        with source.open("r", encoding="utf-8", errors="replace") as src:
            for line in src:
                annotated = HEX.sub(resolve, line) if "0x" in line else line
                if annotated == line:
                    if dst is None:
                        pending.append(line)
                    else:
                        dst.write(line)
                    continue
                if dst is None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    dst = target.open("w", encoding="utf-8")
                    for buffered in pending:
                        dst.write(buffered)
                    pending.clear()
                dst.write(annotated)
    finally:
        if dst is not None:
            dst.close()
    return dst is not None


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
        if _annotate_stream(out, out.with_suffix(".annotated.txt"), starts, entries):
            touched += 1
    return touched
