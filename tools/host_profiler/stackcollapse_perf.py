#!/usr/bin/env python3
"""Minimal perf script -> folded stacks (FlameGraph format).

Frames without a resolved symbol keep their module ("[libmonobdwgc-2.0.so]")
instead of collapsing into a single flat [unknown]; module attribution is the
difference between a useless flame and a triage-able one.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from contextlib import nullcontext
from pathlib import PurePath
from typing import IO

# sample header: "comm pid/tid [cpu] timestamp: ..."
HEADER = re.compile(r"^(\S+)\s+(\d+)(?:/(\d+))?\s+")
# Leading address is bare hex on default `perf script`, but some versions / -F
# formats prefix it with 0x; accept either.
FRAME = re.compile(r"^(?:0x)?[0-9a-fA-F]+\s+(.*?)(?:\+0x[0-9a-fA-F]+)?\s*(?:\((.*)\))?$")
MAX_FRAME_LEN = 2048  # cap the non-greedy regex's backtracking on a pathological line
MAX_STACK_DEPTH = 4096  # cap per-sample stack growth (corrupt/huge perf.data)


def frame_name(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) > MAX_FRAME_LEN:
        raw = raw[:MAX_FRAME_LEN]
    match = FRAME.match(raw)
    if not match:
        return raw.replace(";", ":")
    symbol = (match.group(1) or "").strip()
    module = (match.group(2) or "").strip()
    if symbol in ("", "[unknown]"):
        if module in ("", "[unknown]"):
            # anonymous executable pages = Mono JIT code outside the exported map
            symbol = "[jit]" if module else "[unknown]"
        else:
            symbol = f"[{PurePath(module).name}]"
    return symbol.replace(";", ":")


def collapse(stream: IO[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    stack: list[str] = []
    in_stack = False

    def flush() -> None:
        nonlocal stack
        if stack:
            counts[";".join(reversed(stack))] += 1  # root -> leaf
        stack = []

    for line in stream:
        line = line.rstrip("\n")
        if not line.strip():
            if in_stack:
                flush()
                in_stack = False
            continue
        if HEADER.match(line) and not line[:1].isspace():
            if in_stack:
                flush()
            in_stack = True
            continue
        if in_stack:
            name = frame_name(line)
            if name and len(stack) < MAX_STACK_DEPTH:
                stack.append(name)
    if in_stack:
        flush()
    return counts


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "-"
    with (
        open(path, encoding="utf-8", errors="replace") if path != "-" else nullcontext(sys.stdin)
    ) as fh:
        counts = collapse(fh)
    for key, value in counts.most_common():
        print(f"{key} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
