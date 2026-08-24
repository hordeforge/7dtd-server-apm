#!/usr/bin/env python3
"""Tiny SVG flamegraph from folded stacks (name;name;name count)."""

from __future__ import annotations

import gc
import hashlib
import math
import sys
import xml.sax.saxutils as xu
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# One iterative-DFS work item over the prefix tree: (node, consumed x at
# entry, remaining sorted siblings, depth).
_TreeDfsFrame = tuple[dict[str, Any], float, "Iterator[tuple[str, dict[str, Any]]]", int]

_COLORS: dict[str, str] = {}


def color(name: str) -> str:
    # Frame names repeat across thousands of rendered rectangles; hash each
    # distinct name once instead of once per node that carries it.
    cached = _COLORS.get(name)
    if cached is not None:
        return cached
    h = hashlib.md5(name.encode("utf-8")).hexdigest()
    r = 200 + int(h[0:2], 16) % 55
    g = 50 + int(h[2:4], 16) % 120
    b = 20 + int(h[4:6], 16) % 50
    _COLORS[name] = result = f"rgb({r},{g},{b})"
    return result


def render(folded: Path, out: Path) -> int:
    # Prefix tree with inclusive totals, grown one child lookup per frame:
    # slicing a fresh prefix tuple per depth (the former accumulation) made
    # every stack O(depth^2) in allocations and hashes, which dominated
    # wall-clock on real folded files (tens of seconds at ~100k deep rows).
    root: dict[str, Any] = {"name": "", "value": 0.0, "children": {}}
    grand = 0.0
    max_depth = 0
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
        node = root
        node["value"] += c
        depth = 0
        for frame in stack:
            child = node["children"].get(frame)
            if child is None:
                child = {"name": frame, "value": 0.0, "children": {}}
                node["children"][frame] = child
            child["value"] += c
            node = child
            depth += 1
        if depth > max_depth:
            max_depth = depth

    if grand <= 0:
        print("empty", file=sys.stderr)
        return 1

    width = 1200
    frame_h = 16
    height = 40 + (max_depth + 2) * frame_h

    def sorted_children(node: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        # Heaviest sibling first; the stable sort keeps insertion (first-seen)
        # order for ties, matching the former prefix-tuple table's ordering.
        return iter(sorted(node["children"].items(), key=lambda kv: -kv[1]["value"]))

    # Iterative DFS laying x offsets left to right: stack depth in the folded
    # file is unbounded input, and the recursive form hit CPython's recursion
    # limit on deep stacks. Each frame carries its own running x, mirroring
    # place(); every child advances its parent's running x even when too
    # narrow to render, so wider later siblings land where the former
    # table-driven layout put them.
    # Subtrees narrower than the 0.5 px render floor are not entered: child
    # widths shrink monotonically (a descendant's inclusive total can only be
    # smaller), so nothing below the floor ever produces a rectangle. Deep
    # folded inputs have millions of such leaf-side prefixes; entering each
    # just to sort empty children dominated wall-clock.
    rects: list[str] = []
    append_rect = rects.append
    work: list[_TreeDfsFrame] = [(root, 0.0, sorted_children(root), 0)]
    while work:
        parent, x, children, depth = work[-1]
        child = next(children, None)
        if child is None:
            work.pop()
            continue
        name, subtree = child
        child_x = float(x)
        child_w = width * (subtree["value"] / grand)
        work[-1] = (parent, x + child_w, children, depth)
        if child_w < 0.5:
            continue
        work.append((subtree, child_x, sorted_children(subtree), depth + 1))
        val = subtree["value"]
        y = height - 20 - (depth + 1) * frame_h
        pct = 100.0 * val / grand
        title = f"{name} ({pct:.2f}%, n={val:.0f})"
        append_rect(
            f"<g><title>{xu.escape(title)}</title>"
            f'<rect x="{child_x:.2f}" y="{y}" width="{child_w:.2f}" '
            f'height="{frame_h - 1}" fill="{color(name)}"/>'
            f'<text x="{child_x + 2:.2f}" y="{y + 12}" font-size="11" '
            f'font-family="monospace">'
            f"{xu.escape(name[: max(1, int(child_w / 7))])}</text></g>"
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


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: flamegraph.py stacks.folded out.svg", file=sys.stderr)
        return 2
    # The tree build allocates one dict per distinct stack prefix - millions on
    # a deep folded file. Every cyclic-GC threshold pass then re-scans that
    # whole growing heap, which is where the wall-clock actually went (22s vs
    # 2s measured on the same input). Nothing below creates reference cycles,
    # so pure refcounting reclaims as we go; restore the collector afterward so
    # embedders (the test suite calls main directly) keep their GC state.
    gc.disable()
    try:
        return render(Path(sys.argv[1]), Path(sys.argv[2]))
    finally:
        gc.enable()


if __name__ == "__main__":
    raise SystemExit(main())
