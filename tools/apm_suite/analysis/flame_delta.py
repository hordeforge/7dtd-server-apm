"""Frame-weight deltas between two stacks.folded files."""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def folded_stack_path(session: Path) -> Path | None:
    """The session's folded stacks: annotated twin first, then the raw capture,
    then a root-level file from the legacy session layout. One resolver for
    every consumer (session compare, flame diff HTML) so their candidate lists
    cannot drift into disagreeing about which evidence to read."""
    for rel in ("cpu/perf/stacks.annotated.folded", "cpu/perf/stacks.folded", "stacks.folded"):
        path = session / rel
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def load_weights(path: Path) -> dict[str, int]:
    # Streamed line by line: folded inputs reach hundreds of MB and session
    # compare loads two at once, so read-all + splitlines held several resident
    # copies of each file for nothing (same rationale as annotate_stacks).
    weights: dict[str, int] = defaultdict(int)
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                stack, raw_count = line.rsplit(" ", 1)
                value = float(raw_count)
            except ValueError:
                continue
            # int(float("inf")) raises OverflowError; a non-finite weight is not a
            # real sample count, so skip it rather than crash the whole load.
            if not math.isfinite(value):
                continue
            count = int(value)
            for frame in stack.split(";"):
                if frame:
                    weights[frame] += count
    return dict(weights)


def delta(a: dict[str, int], b: dict[str, int], top: int = 30) -> list[dict[str, Any]]:
    # Iterate a deterministic name order, not set(a) | set(b): str hashing is
    # per-process randomized, so equal-|delta| frames would survive the stable
    # sort into a run-dependent top-N and make compare output irreproducible.
    rows: list[dict[str, Any]] = [
        {
            "frame": name,
            "a": a.get(name, 0),
            "b": b.get(name, 0),
            "delta": b.get(name, 0) - a.get(name, 0),
        }
        for name in sorted(set(a) | set(b))
    ]
    rows.sort(key=lambda r: (-abs(int(r["delta"])), str(r["frame"])))
    return rows[:top]
