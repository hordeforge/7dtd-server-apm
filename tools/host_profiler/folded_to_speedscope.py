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
from pathlib import Path


def load_folded(path: Path) -> list[tuple[list[str], int]]:
    rows: list[tuple[list[str], int]] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        # last token is count
        try:
            stack_s, count_s = line.rsplit(" ", 1)
            count = int(float(count_s))
        except ValueError:
            continue
        if count <= 0:
            continue
        frames = [f for f in stack_s.split(";") if f]
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

    def freeze(node: dict) -> dict:
        kids = node["children"]
        out = {"name": node["name"], "value": node["value"]}
        if kids:
            out["children"] = [
                freeze(kids[k]) for k in sorted(kids.keys(), key=lambda x: -kids[x]["value"])
            ]
        return out

    return freeze(root)


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
    tree_path.write_text(json.dumps(to_d3_tree(rows)))
    print(f"wrote {tree_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
