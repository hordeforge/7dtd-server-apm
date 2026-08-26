"""Deterministic fuzz targets for the untrusted profiler-text parsers.

Four surfaces ingest text nobody controls at analysis time: `perf script`
output (format varies across perf versions, perf.data can be corrupt),
folded-stack files and speedscope profiles (they arrive inside imported
evidence bundles produced by other people), and every collector artifact the
event timeline re-reads (bpftrace text dumps, proc/threads/bridge JSONL, the
bridge's apm_app.json). A crash or hang there destroys the whole session's
analysis.

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
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from apm_suite.analysis.bridge import load_speedscope_frames, parse_section_line
from apm_suite.analysis.events import PER_SOURCE_MAX, RETAINED_MAX, build_timeline
from apm_suite.models import EventsV2, schema_dict
from apm_suite.paths import REPO


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


# --- apm_suite session-artifact parsers (imported evidence bundles) --------------

# Scalars planted into collector fields by corrupt writers, older schemas, and
# hand edits. Includes the shapes that historically crashed the readers:
# multi-dot "1.2.3" numbers, overlong digit runs (json.loads -> inf), bools
# (float() accepts them; they are not measurements), and nested containers.
SCALARS: list[Any] = [
    1,
    0,
    -3,
    12.5,
    "12.5",
    "abc",
    "",
    None,
    True,
    False,
    [1],
    {"a": 1},
    10**400,
    "9" * 400,
]
UTC_STAMPS = [
    "2026-01-01T00:00:00Z",
    "2026-01-02T03:04:05+02:00",
    "2026-01-03T00:00:00",  # naive: UTC by repo convention
    "not-a-date",
    "",
    None,
    5,
]
WORLDS: list[Any] = [{"entities": 7}, {"entities": "x"}, {}, "x", [1], None, 0]
WCHAN_NAMES = ["futex_wait_queue", "wait_woken", "0", "-", "unix_stream", "\x00bad", "x" * 60]

SESSION_SEEDS = range(4)


def _spike(rng: random.Random) -> Any:
    if rng.random() < 0.8:
        return {
            "gmUpdateDurationMs": rng.choice(SCALARS),
            "serverTickIntervalMs": rng.choice(SCALARS),
            "utc": rng.choice(UTC_STAMPS),
            "world": rng.choice(WORLDS),
        }
    return rng.choice(SCALARS)  # non-dict entry among spikes[]


def _app_doc(rng: random.Random) -> Any:
    roll = rng.random()
    if roll < 0.75:
        doc: dict[str, Any] = {"schema": "7dtd.apm.app.v3"}
        spikes = [_spike(rng) for _ in range(rng.randint(0, 6))]
        # A non-list spikes value is a format change, not an exception.
        doc["spikes"] = spikes if roll < 0.65 else rng.choice([{"a": 1}, "x", None])
        return doc
    # Valid-JSON non-object roots must read as absent evidence, never raise.
    return rng.choice([[], 5, "x", None, True])


def _threads_record(rng: random.Random) -> Any:
    roll = rng.random()
    if roll > 0.92:
        return rng.choice(["torn {", "", "[]", "[1,2]"])  # torn / non-object lines
    wchan_roll = rng.random()
    if wchan_roll < 0.7:
        wchan: Any = {
            rng.choice(WCHAN_NAMES): rng.choice(SCALARS) for _ in range(rng.randint(0, 8))
        }
    else:
        wchan = rng.choice([["futex_wait", 7], "futex", 5, None])  # truthy non-dicts
    return {"t": rng.choice(SCALARS), "wchan_top": wchan}


def _proc_record(rng: random.Random) -> dict[str, Any]:
    return {
        "t": rng.choice(SCALARS),
        "cpu_pct": rng.choice(SCALARS),
        "rss_mb": rng.choice(SCALARS),
    }


def _scrape_record(rng: random.Random) -> dict[str, Any]:
    text_pool = [
        f"gmUpdateDuration={rng.choice(['250.5', '1.2.3', 'abc'])}ms spike detected",
        f"avg={rng.choice(['33.1', '99', '9' * 400])}ms tick",
        "".join(rng.choice(NASTY) for _ in range(rng.randint(1, 3))),
        "plain console noise with player names",
    ]
    return {"t": rng.choice(SCALARS), "text": rng.choice(text_pool)}


def _bt_text(rng: random.Random) -> str:
    lines: list[str] = []
    for _ in range(rng.randint(0, 60)):
        roll = rng.random()
        if roll < 0.25:
            lines.append(f"@wait_n: {rng.choice(['40', '-2', 'abc', '9' * 400])}\n")
        elif roll < 0.6:
            marker = rng.choice(
                ["SLOW_", "SLOW ", "STALL_MAIN", "STW_PAUSE", "@little_n: 3", "@gc_n: x"]
            )
            payload = "".join(rng.choice(NASTY) for _ in range(rng.randint(0, 3)))
            lines.append(f"{marker} {payload} {rng.choice(COUNTS)}\n")
        else:
            lines.append("".join(rng.choice(NASTY) for _ in range(rng.randint(1, 3))) + "\n")
    return "".join(lines)


@pytest.mark.parametrize("seed", list(SESSION_SEEDS))
def test_fuzz_events_timeline(tmp_path: Path, seed: int) -> None:
    """The event timeline re-reads every collector artifact from imported
    bundles without schema guarantees; malformed shapes must degrade to
    absent evidence and still produce a valid, deterministic EventsV2."""
    rng = random.Random(seed)
    session = tmp_path / f"session_{seed}"
    app = session / "app"
    threads_dir = session / "threads"
    memory = session / "memory"
    sync = session / "sync"
    for directory in (app, threads_dir, memory, sync):
        directory.mkdir(parents=True)

    (app / "apm_app.json").write_text(json.dumps(_app_doc(rng)), encoding="utf-8")
    (app / "bridge.jsonl").write_text(
        "".join(json.dumps(_scrape_record(rng)) + "\n" for _ in range(rng.randint(0, 30))),
        encoding="utf-8",
    )
    (threads_dir / "threads.jsonl").write_text(
        "".join(json.dumps(_threads_record(rng)) + "\n" for _ in range(rng.randint(0, 30))),
        encoding="utf-8",
    )
    (memory / "proc.jsonl").write_text(
        "".join(json.dumps(_proc_record(rng)) + "\n" for _ in range(rng.randint(0, 30))),
        encoding="utf-8",
    )
    (sync / "futex.bt.out").write_text(_bt_text(rng), encoding="utf-8")

    first = build_timeline(session)
    second = build_timeline(session)
    assert first == second, f"seed={seed}: timeline must be deterministic"

    # CHECK-constraint identities the model validator also enforces.
    assert first.count == sum(first.by_kind.values())
    assert first.retained == len(first.events) <= RETAINED_MAX
    assert first.dropped == first.count - first.retained >= 0

    per_source = Counter(event.source for event in first.events)
    assert all(count <= PER_SOURCE_MAX for count in per_source.values()), (
        f"seed={seed}: per-source retention bound exceeded"
    )

    # Serialization boundary: events persist as strict JSON, so the document
    # must encode without leaking NaN/Infinity/surrogates and read back equal.
    encoded = json.dumps(schema_dict(first), allow_nan=False)
    restored = EventsV2.model_validate(json.loads(encoded))
    assert restored == first


SECTION_SEEDS = range(4)
SECTION_NUMBERS = ["12.50", "0", "400", "1.2.3", "9" * 400, ".5", "5.", "007"]
SECTION_NAMES = ["World.TickEntities", "EntityAlive.updateTasks", "A.B.C_d", "x" * 200]


def _section_text(rng: random.Random) -> str:
    parts: list[str] = []
    for _ in range(rng.randint(0, 30)):
        roll = rng.random()
        num = rng.choice(SECTION_NUMBERS)
        name = rng.choice(SECTION_NAMES)
        if roll < 0.55:
            parts.append(f"{name}={num}ms(x{num},max={num}) ")
        elif roll < 0.75:
            parts.append(f"avg={num}ms ")
        else:
            parts.append("".join(rng.choice(NASTY) for _ in range(rng.randint(1, 3))) + " ")
    return "".join(parts)


def _speedscope_doc(rng: random.Random) -> Any:
    roll = rng.random()
    if roll < 0.1:
        return rng.choice([[1, 2], "root", 5, None])  # non-object roots
    names = ["GameManager.gmUpdate", "[unknown]", "", "Entité★"]

    def frame() -> Any:
        if rng.random() < 0.85:
            return {"name": rng.choice(names)}
        return rng.choice(["bare", 7, None])  # non-dict frame entries

    doc: dict[str, Any] = {
        "shared": {"frames": [frame() for _ in range(rng.randint(0, 5))]},
        "profiles": [],
    }
    if rng.random() < 0.15:
        doc["shared"] = rng.choice(["x", None, 5, []])
    for _ in range(rng.randint(0, 3)):
        samples: list[Any] = []
        for _ in range(rng.randint(0, 8)):
            if rng.random() < 0.7:
                samples.append([rng.randint(-1, 6) for _ in range(rng.randint(0, 4))])
            else:
                samples.append(rng.choice([[0, "x"], "abc", 3, [True], [[0]], None]))
        profile: dict[str, Any] = {"samples": samples}
        if rng.random() < 0.8:
            profile["weights"] = [
                rng.choice([1, 2.5, "3", "abc", float("inf"), float("nan"), None, True])
                for _ in samples
            ]
        doc["profiles"].append(profile)
    if rng.random() < 0.15:
        doc["profiles"] = rng.choice(["x", 5, [1, "a"]])
    return doc


@pytest.mark.parametrize("seed", list(SECTION_SEEDS))
def test_fuzz_managed_sections_surface(tmp_path: Path, seed: int) -> None:
    """Managed section rows arrive as console scrapes and speedscope profiles
    as unvalidated JSON/text from imported bundles; every survivor must be
    well-typed and every run deterministic."""
    text = _section_text(random.Random(seed))

    sections_a = parse_section_line(text)
    sections_b = parse_section_line(text)
    assert sections_a == sections_b, f"seed={seed}: section parse must be deterministic"
    for section in sections_a:
        assert isinstance(section["name"], str) and section["name"]
        avg = section["avgMs"]
        max_ms = section["maxMs"]
        assert type(avg) is float and math.isfinite(avg) and avg >= 0, (
            f"seed={seed}: bad avgMs {avg!r}"
        )
        assert type(max_ms) is float and math.isfinite(max_ms) and max_ms >= 0, (
            f"seed={seed}: bad maxMs {max_ms!r}"
        )
        calls = section["calls"]
        assert type(calls) is int and calls >= 0, f"seed={seed}: bad calls {calls!r}"

    profile_path = tmp_path / "cpu/perf/profile.speedscope.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(_speedscope_doc(random.Random(seed + 100))), encoding="utf-8"
    )

    frames_a = load_speedscope_frames(tmp_path)
    frames_b = load_speedscope_frames(tmp_path)
    assert frames_a == frames_b, f"seed={seed}: speedscope load must be deterministic"
    weights = [weight for _, weight in frames_a]
    assert weights == sorted(weights, reverse=True), f"seed={seed}: frame ranking violated"
    for name, weight in frames_a:
        assert isinstance(name, str), f"seed={seed}: non-string frame name {name!r}"
        assert type(weight) is int and weight >= 0, f"seed={seed}: bad weight {weight!r}"


def test_fuzz_session_artifacts_regression(tmp_path: Path) -> None:
    """Regression artifacts: each of these raised out of the required analysis
    stages before hardening (AttributeError on non-dict roots/nested values,
    ValueError on multi-dot numbers)."""
    session = tmp_path / "session_regr"

    app = session / "app"
    app.mkdir(parents=True)
    # world=str crashed the message build; a scalar among spikes[] crashed
    # spike.get(); an out-of-range integer "t" crashed the timeline sort.
    # The document root stays an object: the bridge writes one snapshot, so a
    # list root is foreign evidence and is covered separately below.
    (app / "apm_app.json").write_text(
        '{"spikes":[{"gmUpdateDurationMs":300,"utc":"2026-01-01T00:00:00Z","world":"x"},'
        "5,"
        '{"gmUpdateDurationMs":250,"world":{"entities":3}}]}',
        encoding="utf-8",
    )
    threads_dir = session / "threads"
    threads_dir.mkdir(parents=True)
    # wchan_top as list/scalar crashed .items(); the 10**400 stamp on the last
    # row crashed the timeline's float() with OverflowError (JSON integers are
    # unbounded), taking the whole required events stage down with it.
    (threads_dir / "threads.jsonl").write_text(
        '{"t":1,"wchan_top":["futex_wait",7]}\n{"t":2,"wchan_top":"futex"}\n'
        '{"t":3,"wchan_top":{"futex_wait":5}}\n'
        f'{{"t":{10**400},"wchan_top":{{"futex_wait":9}}}}\n',
        encoding="utf-8",
    )
    doc = build_timeline(session)
    kinds = {event.kind for event in doc.events}
    assert "wchan" in kinds and "frame_spike" in kinds

    # A list-rooted snapshot is foreign evidence (the bridge writes one object),
    # so it reads as absent rather than raising or inventing spikes.
    listed = tmp_path / "session_listed"
    (listed / "app").mkdir(parents=True)
    (listed / "app/apm_app.json").write_text(
        '[{"spikes":[{"gmUpdateDurationMs":300,"utc":"2026-01-01T00:00:00Z"}]}]',
        encoding="utf-8",
    )
    assert not [event for event in build_timeline(listed).events if event.kind == "frame_spike"]

    # "1.2.3ms" crashed parse_section_line's bare float(); the mangled row is
    # skipped while its valid sibling survives.
    sections = parse_section_line("Foo=1.2.3ms(x2,max=3.4.5) Bar=6.5ms(x2,max=7.5)")
    assert sections == [{"name": "Bar", "avgMs": 6.5, "calls": 2, "maxMs": 7.5}]

    # Non-dict roots / string frames crashed load_speedscope_frames; the valid
    # profile shape keeps aggregating exactly as before (1e999 parses to inf,
    # whose sample is dropped like any non-finite weight).
    profile = tmp_path / "speedscope"
    perf_dir = profile / "cpu/perf"
    perf_dir.mkdir(parents=True)
    target = perf_dir / "profile.speedscope.json"
    target.write_text(
        '{"shared":{"frames":[{"name":"a"},{"name":"b"}]},'
        '"profiles":[{"samples":[[0],[1],[0]],"weights":[1,2,1e999]}]}',
        encoding="utf-8",
    )
    assert load_speedscope_frames(profile) == [("b", 2), ("a", 1)]
    target.write_text("[1,2]")
    assert load_speedscope_frames(profile) == []
    target.write_text('{"shared":"x"}')
    assert load_speedscope_frames(profile) == []
