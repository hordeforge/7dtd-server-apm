#!/usr/bin/env python3
"""Convert stacks.folded (name;name;name count) to Speedscope JSON.

Usage:
  python3 folded_to_speedscope.py stacks.folded -o profile.speedscope.json
  npx speedscope profile.speedscope.json
  # or drag-drop onto https://www.speedscope.app/
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

# Folded files can come from outside this pipeline (imported evidence bundles),
# where a single row may claim thousands of frames. Bound per-stack growth to
# the same corrupt-input ceiling stackcollapse_perf.MAX_STACK_DEPTH applies, so
# every downstream tree builder and serializer sees bounded depth.
MAX_STACK_DEPTH = 4096


def load_folded(path: Path) -> list[tuple[list[str], int]]:
    rows: list[tuple[list[str], int]] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        # last token is count
        try:
            stack_s, count_s = line.rsplit(" ", 1)
            value = float(count_s)
        except ValueError:
            continue
        # int(float("inf")) raises OverflowError; a non-finite count is not a
        # real sample count, so skip it rather than crash the conversion.
        if not math.isfinite(value):
            continue
        count = int(value)
        if count <= 0:
            continue
        frames = [f for f in stack_s.split(";") if f][:MAX_STACK_DEPTH]
        if not frames:
            continue
        rows.append((frames, count))
    return rows


def to_speedscope(rows: list[tuple[list[str], int]], name: str = "CPU") -> dict:
    frame_index: dict[str, int] = {}
    frames: list[dict] = []

    def idx(name: str) -> int:
        if name not in frame_index:
            frame_index[name] = len(frames)
            frames.append({"name": name})
        return frame_index[name]

    samples: list[list[int]] = []
    weights: list[int] = []
    total = 0
    for stack, count in rows:
        samples.append([idx(f) for f in stack])
        weights.append(count)
        total += count

    return {
        "$schema": "https://www.speedscope.app/file-format-schema.json",
        "exporter": "7dtd-folded_to_speedscope",
        "name": name,
        "activeProfileIndex": 0,
        "shared": {"frames": frames},
        "profiles": [
            {
                "type": "sampled",
                "name": name,
                "unit": "samples",
                "startValue": 0,
                "endValue": total,
                "samples": samples,
                "weights": weights,
            }
        ],
    }


def to_d3_tree(rows: list[tuple[list[str], int]], root_name: str = "all") -> dict:
    """Hierarchical tree for d3-flame-graph / our interactive HTML."""
    root: dict = {"name": root_name, "value": 0, "children": {}}

    def ensure(node: dict, name: str) -> dict:
        ch = node["children"]
        if name not in ch:
            ch[name] = {"name": name, "value": 0, "children": {}}
        return ch[name]

    for stack, count in rows:
        node = root
        root["value"] += count
        for frame in stack:
            node = ensure(node, frame)
            node["value"] += count

    # Post-order freeze with an explicit worklist: tree depth equals stack
    # depth (bounded only by MAX_STACK_DEPTH), far past the interpreter's
    # recursion limit, so the recursive form crashed on deep folded rows.
    frozen: dict[int, dict] = {}
    work: list[tuple[dict, bool]] = [(root, False)]
    while work:
        node, expanded = work.pop()
        if expanded:
            out = {"name": node["name"], "value": node["value"]}
            kids = node["children"]
            if kids:
                ordered = sorted(kids.keys(), key=lambda x: -kids[x]["value"])
                out["children"] = [frozen[id(kids[k])] for k in ordered]
            frozen[id(node)] = out
            continue
        work.append((node, True))
        for child in node["children"].values():
            work.append((child, False))
    return frozen[id(root)]


class _Close:
    """Pop marker closing a JSON container opened by dumps_deep."""

    __slots__ = ("char",)

    def __init__(self, char: str) -> None:
        self.char = char


class _Raw:
    """Already-encoded JSON text emitted verbatim by dumps_deep."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def dumps_deep(value: object) -> str:
    """json.dumps without CPython's recursive-encoder depth limit.

    Flame trees nest once per stack frame; json.dumps raises RecursionError
    well before MAX_STACK_DEPTH. Container traversal here is iterative while
    every scalar still goes through json.dumps, so escaping and number
    formatting match ``json.dumps(value, separators=(",", ":"))``
    byte-for-byte (asserted by the fuzz suite). Dict keys must be strings;
    anything else raises TypeError instead of guessing at coercion.
    """
    parts: list[str] = []
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, _Close):
            parts.append(item.char)
        elif isinstance(item, _Raw):
            parts.append(item.text)
        elif isinstance(item, dict):
            parts.append("{")
            stack.append(_Close("}"))
            pairs = list(item.items())
            # Push in reverse so pops emit insertion order (byte-parity with
            # json.dumps); the last pair pushed carries no trailing separator.
            for offset in range(len(pairs)):
                key, val = pairs[len(pairs) - 1 - offset]
                if not isinstance(key, str):
                    raise TypeError(f"dumps_deep supports string keys only, got {key!r}")
                stack.append(val)
                # Separators ride the stack as verbatim markers; pushing them as
                # plain strings would reach the scalar branch and get encoded.
                stack.append(_Raw(":"))
                stack.append(key)
                if offset < len(pairs) - 1:
                    stack.append(_Raw(","))
        elif isinstance(item, (list, tuple)):
            parts.append("[")
            stack.append(_Close("]"))
            for offset, val in enumerate(reversed(item)):
                stack.append(val)
                if offset < len(item) - 1:
                    stack.append(_Raw(","))
        else:
            parts.append(json.dumps(item))
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folded", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True, help="*.speedscope.json")
    ap.add_argument("--name", default="7DTD CPU")
    ap.add_argument("--tree", type=Path, default=None, help="also write d3 tree JSON")
    args = ap.parse_args()

    if not args.folded.is_file():
        print(f"missing {args.folded}")
        return 1
    rows = load_folded(args.folded)
    if not rows:
        print("no stacks")
        return 1

    prof = to_speedscope(rows, args.name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(prof))
    print(
        f"wrote {args.output} samples={sum(w for _, w in rows)} stacks={len(rows)} frames={len(prof['shared']['frames'])}"
    )

    tree_path = args.tree
    if tree_path is None:
        tree_path = args.output.with_suffix("").with_suffix(".tree.json")
        if str(args.output).endswith(".speedscope.json"):
            tree_path = Path(str(args.output).replace(".speedscope.json", ".tree.json"))
    tree_path.write_text(dumps_deep(to_d3_tree(rows)))
    print(f"wrote {tree_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
