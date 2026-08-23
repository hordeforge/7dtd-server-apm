#!/usr/bin/env python3
"""Tiny SVG flamegraph from folded stacks (name;name;name count)."""

from __future__ import annotations

import hashlib
import math
import sys
import xml.sax.saxutils as xu
from collections import defaultdict
from pathlib import Path


def color(name: str) -> str:
    h = hashlib.md5(name.encode("utf-8")).hexdigest()
    r = 200 + int(h[0:2], 16) % 55
    g = 50 + int(h[2:4], 16) % 120
    b = 20 + int(h[4:6], 16) % 50
    return f"rgb({r},{g},{b})"


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: flamegraph.py stacks.folded out.svg", file=sys.stderr)
        return 2
    folded = Path(sys.argv[1])
    out = Path(sys.argv[2])
    # node -> total
    totals: dict[tuple[str, ...], float] = defaultdict(float)
    grand = 0.0
    for line in folded.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        *parts, count_s = line.rsplit(" ", 1)
        # A malformed collapsed line (missing/non-numeric count) must not crash
        # the whole flamegraph; skip it. Drop empty frames from ";;" runs.
        try:
            c = float(count_s)
        except ValueError:
            continue
        # NaN/inf would poison every downstream coordinate and serialize a
        # broken SVG ("nan" widths); skip non-finite counts.
        if not math.isfinite(c):
            continue
        stack = [f for f in parts[0].split(";") if f] if parts else []
        if not stack:
            continue
        grand += c
        for i in range(len(stack)):
            totals[tuple(stack[: i + 1])] += c

    if grand <= 0:
        print("empty", file=sys.stderr)
        return 1

    width = 1200
    frame_h = 16
    max_depth = max((len(k) for k in totals), default=1)
    height = 40 + (max_depth + 2) * frame_h

    # layout x offsets per depth using sorted siblings
    by_parent: dict[tuple[str, ...], list[tuple[str, ...]]] = defaultdict(list)
    for key in totals:
        by_parent[key[:-1]].append(key)
    for p in by_parent:
        by_parent[p].sort(key=lambda k: -totals[k])

    x_of: dict[tuple[str, ...], float] = {(): 0.0}

    # Iterative DFS over the prefix tree: stack depth in the folded file is
    # unbounded input, and the recursive form hit CPython's recursion limit on
    # deep stacks. Each frame carries its own running x, mirroring place().
    root_frame: list[object] = [(), 0.0, iter(by_parent.get((), []))]
    work: list[list[object]] = [root_frame]
    while work:
        frame = work[-1]
        parent, x, children = frame
        child = next(children, None)
        if child is None:
            work.pop()
            continue
        assert isinstance(parent, tuple)
        x_of[child] = float(x)
        work.append([child, float(x), iter(by_parent.get(child, []))])
        frame[1] = float(x) + width * (totals[child] / grand)

    rects = []
    for key, val in totals.items():
        if not key:
            continue
        w = width * (val / grand)
        if w < 0.5:
            continue
        depth = len(key)
        x = x_of.get(key, 0)
        y = height - 20 - depth * frame_h
        name = key[-1]
        pct = 100.0 * val / grand
        title = f"{name} ({pct:.2f}%, n={val:.0f})"
        rects.append(
            f"<g><title>{xu.escape(title)}</title>"
            f'<rect x="{x:.2f}" y="{y}" width="{w:.2f}" height="{frame_h - 1}" fill="{color(name)}"/>'
            f'<text x="{x + 2:.2f}" y="{y + 12}" font-size="11" font-family="monospace">'
            f"{xu.escape(name[: max(1, int(w / 7))])}</text></g>"
        )

    svg = [
        # Accessible name/description for the whole picture (WCAG 1.1.1):
        # screen readers announce the graphic instead of an unlabeled svg.
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'role="img" aria-labelledby="flame-title flame-desc">',
        '<rect width="100%" height="100%" fill="#f8f8f8"/>',
        '<title id="flame-title">7dtd flamegraph</title>',
        f'<desc id="flame-desc">Flamegraph of {grand:.0f} samples; each rectangle is a stack frame, '
        "width is its share of samples, depth is the call-stack level.</desc>",
        f'<text x="10" y="20" font-size="14" font-family="sans-serif">7dtd flamegraph (samples={grand:.0f})</text>',
        *rects,
        "</svg>",
    ]
    out.write_text("\n".join(svg), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
