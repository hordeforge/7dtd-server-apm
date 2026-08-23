"""Deterministic fuzz targets for the untrusted profiler-text parsers.

Two surfaces ingest text nobody controls at analysis time: `perf script`
output (format varies across perf versions, perf.data can be corrupt) and
folded-stack files (they arrive inside imported evidence bundles produced by
other people). A crash or hang there destroys the whole session's analysis.

Each target is structure-aware: rows are generated from the real formats,
then mutated across fixed seeds, so any failure reproduces exactly from the
seed printed in the assertion message. Every case asserts invariants instead
of only "did not raise" (a fuzzer proves presence of bugs, not absence) and
pushes data through the full pipeline including its serialization boundary,
so correctness and encoding bugs become live failures here.
"""

from __future__ import annotations

import importlib.util
import io
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).parents[3]


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO / f"tools/host_profiler/{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SYMBOLS = [
    "GameManager.gmUpdate",
    "[unknown]",
    "EntityAlive.updateTasks",
    "GC_malloc",
    "Entité★函数",
    "semi;colons;inside",
    "tab\tinside",
    "",
]
MODULES = [
    "/usr/lib/libmonobdwgc-2.0.so",
    "[unknown]",
    "/tmp/perf-1.map",
    "libc.so.6",
    "",
]
NASTY = ["\x00", "\r", "</script>", "%s%s%n", "a" * 300, ";;;", "\U0001f600", "'", "\\"]
COUNTS = [
    "12",
    "1e3",
    "inf",
    "-inf",
    "nan",
    "-3",
    "0",
    "abc",
    "0x10",
    "999999999999999999999999",
    "5.",
    "+7",
    "1_000",
]

PERF_SEEDS = range(5)
FOLDED_SEEDS = range(5)


def _perf_script(rng: random.Random) -> str:
    """A mutated but perf-script-shaped stream (headers, frames, garbage)."""
    lines: list[str] = []
    depth_left = rng.randint(0, 4500)
    target_lines = rng.randint(1, 300)
    while len(lines) < target_lines:
        roll = rng.random()
        if roll < 0.25:
            ids = f"{rng.randint(1, 2**31)}"
            if rng.random() < 0.8:
                ids += f"/{rng.randint(1, 2**31)}"  # pid/tid form
            lines.append(
                f"7DaysToDieServe {ids} [00{rng.randint(0, 9)}] {rng.random():.6f}: cycles:\n"
            )
        elif roll < 0.75:
            indent = rng.choice(["\t", "  ", "\t "])
            addr = rng.choice([f"{rng.getrandbits(48):x}", f"0x{rng.getrandbits(32):x}"])
            symbol = rng.choice(SYMBOLS)
            module = rng.choice(MODULES)
            offset = f"+0x{rng.randrange(64):x}" if rng.random() < 0.5 else ""
            lines.append(f"{indent}{addr} {symbol}{offset} ({module})\n")
            depth_left -= 1
            if depth_left > 0 and rng.random() < 0.02:
                # pathological depth: past the parser's MAX_STACK_DEPTH cap
                lines.extend("\t7f00 [unknown] ([unknown])\n" for _ in range(depth_left))
                depth_left = 0
        elif roll < 0.85:
            lines.append("\n")
        elif roll < 0.95:
            lines.append("".join(rng.choice(NASTY) for _ in range(rng.randint(1, 4))) + "\n")
        else:
            # header-shaped line indented like a frame must stay a frame
            lines.append(f"\tsrv {rng.randint(1, 9)}/{rng.randint(1, 9)} [000] 1.0:\n")
    if rng.random() < 0.05:
        lines.append("x" * rng.randint(2048, 4096))
    return "".join(lines)


@pytest.mark.parametrize("seed", list(PERF_SEEDS))
def test_fuzz_stackcollapse_perf_script(seed: int) -> None:
    module = _load("stackcollapse_perf")
    stream = _perf_script(random.Random(seed))

    first = module.collapse(io.StringIO(stream))
    second = module.collapse(io.StringIO(stream))
    assert first == second, f"seed={seed}: collapse must be deterministic"
    assert isinstance(first, Counter)
    for stack, count in first.items():
        assert isinstance(stack, str) and stack, f"seed={seed}: empty fold key"
        assert "\n" not in stack
        # ';' is the fold separator: frame names have theirs replaced by ':',
        # so every separator must sit between two non-empty names.
        segments = stack.split(";")
        assert all(segments), f"seed={seed}: empty frame in fold key {stack!r}"
        assert type(count) is int and count > 0


def test_fuzz_stackcollapse_golden_fold() -> None:
    """Pin the exact fold semantics the fuzz generator mutates around."""
    module = _load("stackcollapse_perf")
    counts = module.collapse(
        io.StringIO(
            "srv 1/1 [000] 1.0: cycles:\n"
            "\t7f01 GameManager.gmUpdate+0x42 (/tmp/perf-1.map)\n"
            "\t7f02 [unknown] (/usr/lib/libmonobdwgc-2.0.so)\n"
            "\n"
            "srv 1/1 [000] 2.0: cycles:\n"
            "\tbad line without address\n"
        )
    )
    assert dict(counts) == {
        # perf lists leaf first; collapse joins reversed so the LAST frame line
        # becomes the folded root.
        "[libmonobdwgc-2.0.so];GameManager.gmUpdate": 1,
        # "bad" lexes as a hex address, so this frame still parses (to
        # "line without address") instead of crashing the sample.
        "line without address": 1,
    }


def _folded_rows(rng: random.Random, path: Path) -> None:
    """Write mutated folded-format rows: valid stacks, hostile counts,
    missing counts, and stacks deeper than MAX_STACK_DEPTH."""
    names = SYMBOLS + MODULES + NASTY
    lines: list[str] = []
    for _ in range(rng.randint(1, 120)):
        roll = rng.random()
        if roll < 0.55:
            depth = rng.randint(1, 12)
            stack = ";".join(rng.choice(names) for _ in range(depth))
            lines.append(f"{stack} {rng.choice(COUNTS)}\n")
        elif roll < 0.65:
            lines.append(f"a;b;c {rng.choice(['', ' '])}\n")  # missing count
        elif roll < 0.75:
            depth = rng.randint(1000, 5000)
            lines.append(";".join(f"f{i}" for i in range(depth)) + f" {rng.randint(1, 9)}\n")
        elif roll < 0.85:
            lines.append(rng.choice(["", "   ", "\t"]) + "\n")
        else:
            lines.append(f"{rng.choice(names)} {rng.choice(COUNTS)}\n")
    path.write_text("".join(lines), encoding="utf-8")


@pytest.mark.parametrize("seed", list(FOLDED_SEEDS))
def test_fuzz_folded_pipeline(tmp_path: Path, seed: int) -> None:
    module = _load("folded_to_speedscope")
    folded = tmp_path / "stacks.folded"
    _folded_rows(random.Random(seed), folded)

    rows = module.load_folded(folded)
    total = sum(count for _, count in rows)
    for frames, count in rows:
        assert frames, f"seed={seed}: kept an empty stack"
        assert len(frames) <= module.MAX_STACK_DEPTH, (
            f"seed={seed}: stack depth unbounded: {len(frames)}"
        )
        assert type(count) is int and count > 0, f"seed={seed}: bad count {count!r}"

    profile = module.to_speedscope(rows)
    weights = profile["profiles"][0]["weights"]
    samples = profile["profiles"][0]["samples"]
    frame_count = len(profile["shared"]["frames"])
    assert profile["profiles"][0]["endValue"] == total
    assert sum(weights) == total
    for sample in samples:
        assert all(0 <= index < frame_count for index in sample)

    tree = module.to_d3_tree(rows)
    assert tree["value"] == total

    # Serialization boundary: the tree must survive its own JSON encoding and
    # read back with the same totals (assert-before-write vs assert-after-read).
    encoded = module.dumps_deep(tree)
    restored = json.loads(encoded)
    assert restored["value"] == tree["value"]
    if rows and max(len(frames) for frames, _ in rows) <= 20:
        # Shallow structures have a byte-parity oracle: the reference encoder.
        assert encoded == json.dumps(tree, separators=(",", ":"))


def test_fuzz_folded_deep_row_regression(tmp_path: Path) -> None:
    """Regression artifact: a 5000-frame folded row raised RecursionError in
    to_d3_tree's recursive freeze and could not be serialized afterwards.
    Imported evidence bundles reach this code with exactly such rows."""
    module = _load("folded_to_speedscope")
    folded = tmp_path / "deep.folded"
    folded.write_text(";".join(f"f{i}" for i in range(5000)) + " 7\n")

    rows = module.load_folded(folded)
    assert len(rows[0][0]) == module.MAX_STACK_DEPTH  # bounded, not fatal

    tree = module.to_d3_tree(rows)
    restored = json.loads(module.dumps_deep(tree))
    assert restored["value"] == 7


def test_fuzz_folded_malformed_counts_regression(tmp_path: Path) -> None:
    """Non-finite, negative, zero, and non-numeric counts are skipped, never
    fatal, and never poison downstream weights."""
    module = _load("folded_to_speedscope")
    folded = tmp_path / "bad.folded"
    folded.write_text("a;b inf\na;b nan\na;b -3\na;b 0\na;b abc\na;b\nkeep;me 5\n")

    rows = module.load_folded(folded)
    assert rows == [(["keep", "me"], 5)]
