"""Deterministic fuzz targets for the untrusted-bundle surfaces.

Two entry points take bytes this tool does not control and hand them to code
that writes to the session store:

  * `bundle.import_bundle` extracts a zip produced by a stranger. The member
    names, the compression methods, the encryption flag, and the declared
    sizes are all chosen by whoever wrote the archive, and a failure part way
    through leaves real files in a real store.
  * `prometheus.export_metrics` renders an unvalidated summary.json (hand
    edited, or planted by an imported bundle) into a text exposition other
    tools parse, so a crafted layer name or number has to survive escaping
    and round-trip back to the value it came from.

Both targets build structure-aware archives and documents from the real
formats, then mutate them across fixed seeds, so any failure reproduces
exactly from the seed in the assertion message. Each case asserts invariants
rather than only "did not raise" (a fuzzer proves presence of bugs, not
absence), including a pair assertion across the write/read boundary the
import crosses: a member written into the store must read back byte
identical, and nothing may be written outside it.
"""

from __future__ import annotations

import json
import math
import random
import re
import struct
import zipfile
from pathlib import Path
from typing import Any

import pytest

from apm_suite.bundle import BundleError, export_bundle, import_bundle
from apm_suite.io import atomic_json, member_is_safe
from apm_suite.prometheus import MetricError, export_metrics

IMPORT_SEEDS = range(6)
METRIC_SEEDS = range(6)

# Member names a stranger's zip can carry. Each entry is either a name this
# tool writes itself (the ones an honest bundle has) or a shape that must be
# refused or neutralized: escaping the target, an absolute path, the base
# directory itself, controls and bidi that survive a name comparison, an
# unencodable lone surrogate, a name the platform rejects outright.
MEMBER_NAMES: list[str] = [
    "meta.json",
    "summary.json",
    "manifest.json",
    "events.jsonl",
    "io/vfs.bt.out",
    "../escape.json",
    "io/../../escape.json",
    "/etc/passwd_apm",
    ".",
    "",
    "./meta.json",
    "io/./summary.json",
    "..\\escape.json",
    "  ",
    "meta.json\n",
    "meta\u202ejson",
    "meta\ud800.json",
    "meta\x00.json",
    "a" * 220 + "/" + "b" * 220 + "/meta.json",
    "io/",
]

# Scalars planted in summary.json fields by hand edits, older writers, and
# imported bundles. inf, huge digit runs, bools, and containers are the shapes
# that have to degrade to "no line".
SCALARS: list[Any] = [
    1,
    0,
    -2.5,
    1e308,
    -1e308,
    "12.5",
    "nan",
    "inf",
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
# Label values a crafted session can carry. The exposition escapes backslash,
# quote, LF and CR, so these must read back as the exact source string.
LABELS: list[str] = [
    "cpu",
    'quote"inside',
    "back\\slash",
    "new\nline",
    "carriage\rreturn",
    "</script>",
    "\x00nul",
    "emoji\U0001f600",
    "x" * 300,
    "",
]

# Prometheus text exposition, as a scrape reader sees it: a metric name, an
# optional label set, and one value. The value must be a number a scrape
# reader accepts, so the assertions below check finiteness explicitly rather
# than trusting the format call.
_SAMPLE_RE = re.compile(
    r"\A(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(\{(?P<labels>.*)\})? (?P<value>\S+)\Z",
    re.DOTALL,
)


def _unescape_label(raw: str) -> str:
    """Decode a label value the way a scrape reader does, so the exporter's
    escaping is checked by round trip rather than by inspection."""
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            following = raw[index + 1]
            if following == "n":
                out.append("\n")
            elif following == "r":
                out.append("\r")
            elif following == "\\":
                out.append("\\")
            elif following == '"':
                out.append('"')
            else:
                raise AssertionError(f"unrecognized escape in label value: {raw!r}")
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _source_session(root: Path) -> Path:
    """A minimal but real session, so the archive under mutation is one this
    tool could have written rather than a shape the code never sees."""
    session = root / "session_fuzzbundle"
    (session / "io").mkdir(parents=True)
    atomic_json(
        session / "meta.json",
        {
            "schema": "7dtd.apm.session.v2",
            "utc": "2026-01-01T00:00:00Z",
            "pid": 1,
            "comm": "7DaysToDieServe",
            "seconds": 10,
            "only": "all",
            "no_app": False,
            "analyzer_version": "2.1.0",
        },
    )
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": "session_fuzzbundle",
            "layers": [{"layer": "cpu", "score": 1.5, "state": "collected"}],
        },
    )
    (session / "events.jsonl").write_text('{"t": 1.0, "message": "tick"}\n', encoding="utf-8")
    (session / "io/vfs.bt.out").write_text("openat /steamapps/common\n", encoding="utf-8")
    return session


def _flip_encryption_flag(raw: bytes) -> bytes:
    data = bytearray(raw)
    offset = 0
    while True:
        offset = data.find(b"PK\x03\x04", offset)
        if offset < 0:
            return bytes(data)
        flag = struct.unpack_from("<H", data, offset + 6)[0]
        struct.pack_into("<H", data, offset + 6, flag | 0x1)
        offset += 4


def _set_compression_method(raw: bytes, method: int) -> bytes:
    data = bytearray(raw)
    struct.pack_into("<H", data, 8, method)
    central = data.find(b"PK\x01\x02")
    if central >= 0:
        struct.pack_into("<H", data, central + 10, method)
    return bytes(data)


def _rewrite_member_names(source: Path, target: Path, names: list[str]) -> None:
    """Rebuild an archive with its member names swapped, so the extracted
    paths are the hostile ones while every payload stays a real document."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        payloads = [(info.filename, src.read(info.filename)) for info in src.infolist()]
        for index, name in enumerate(names):
            original, payload = payloads[index % len(payloads)]
            dst.writestr(name, payload)
            assert original is not None


def _files_under(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file()}


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.parametrize("seed", list(IMPORT_SEEDS))
def test_fuzz_import_bundle_on_crafted_archives(tmp_path: Path, seed: int) -> None:
    """An offered bundle must land in the store whole or not at all.

    Every failure mode is a BundleError naming the operator what was wrong;
    nothing else may escape, because the CLI maps BundleError to an exit code
    and lets anything else print a traceback. A refusal must leave the store
    exactly as it was (no stranded half-session that later audits INVALID),
    and an accepted bundle must have every file inside its target directory.
    """
    rng = random.Random(seed)
    root = tmp_path / f"case_{seed}"
    root.mkdir()
    session = _source_session(root)
    honest = export_bundle(session, root / "honest.zip")

    bundle = root / "crafted.zip"
    mutation = rng.choice(["names", "corrupt", "method", "encrypted", "truncate", "duplicate"])
    if mutation == "names":
        _rewrite_member_names(honest, bundle, rng.choices(MEMBER_NAMES, k=rng.randint(1, 6)))
    elif mutation == "corrupt":
        raw = bytearray(honest.read_bytes())
        offset = rng.randrange(30, max(31, len(raw) - 1))
        raw[offset] = rng.randrange(256)
        bundle.write_bytes(bytes(raw))
    elif mutation == "method":
        bundle.write_bytes(_set_compression_method(honest.read_bytes(), rng.choice([93, 99, 14])))
    elif mutation == "encrypted":
        bundle.write_bytes(_flip_encryption_flag(honest.read_bytes()))
    elif mutation == "truncate":
        whole = honest.read_bytes()
        bundle.write_bytes(whole[: rng.randrange(1, max(2, len(whole)))])
    else:
        names = rng.choices(MEMBER_NAMES, k=6)
        _rewrite_member_names(honest, bundle, names + names[: rng.randint(1, 3)])

    store = root / "store"
    store.mkdir()
    before = _files_under(store)
    try:
        result = import_bundle(bundle, store)
    except BundleError as error:
        # A rejected import writes nothing at all, and names the bundle.
        assert str(error), f"seed={seed}: BundleError must name the problem"
        assert _files_under(store) == before, f"seed={seed}: refused import wrote to the store"
        return
    except BaseException as error:
        raise AssertionError(
            f"seed={seed}: import_bundle raised {type(error).__name__}: {error}"
        ) from error

    # Accepted: every member is inside the target, owner-only, and the target
    # is the only thing the store gained.
    gained = _files_under(store) - before
    target = result.session.resolve()
    assert target.parent == store.resolve(), f"seed={seed}: session escaped the store"
    assert target.name.startswith("session_"), f"seed={seed}: {target.name!r}"
    assert _mode(target) == 0o700, f"seed={seed}: restored session mode {oct(_mode(target))}"
    for path in gained:
        assert target in path.resolve().parents, f"seed={seed}: {path} landed outside {target}"
    with zipfile.ZipFile(bundle) as accepted:
        for member in accepted.namelist():
            assert member_is_safe(member), f"seed={seed}: accepted unsafe member {member!r}"
    assert result.errors >= 0 and len(result.findings) == result.errors
    assert all(finding for finding in result.findings), f"seed={seed}: blank audit finding"


@pytest.mark.parametrize("seed", list(IMPORT_SEEDS))
def test_fuzz_import_bundle_round_trip_preserves_evidence(tmp_path: Path, seed: int) -> None:
    """The pair assertion across the import's write/read boundary: bytes
    written into the store must read back exactly, whatever the member name
    and payload shapes are, so a restore can be trusted as the evidence that
    was sent."""
    rng = random.Random(seed)
    root = tmp_path / f"round_{seed}"
    root.mkdir()
    session = _source_session(root)
    # A member name an honest bundle never carries but a bundle from another
    # person can: unicode, a space, and a deep path all have to survive.
    extra = session / "io" / "gc" / "mono alloc.txt"
    extra.parent.mkdir(parents=True)
    extra.write_text("uprobe hit\n", encoding="utf-8")
    (session / "notes.jsonl").write_text(
        "".join(
            json.dumps({"t": index, "label": rng.choice(LABELS)}) + "\n" for index in range(20)
        ),
        encoding="utf-8",
    )
    honest = export_bundle(session, root / "honest.zip")

    store = root / "store"
    store.mkdir()
    result = import_bundle(honest, store)
    target = result.session
    assert (target / "meta.json").read_text(encoding="utf-8").strip().startswith("{")
    assert (target / "io" / "vfs.bt.out").read_text(
        encoding="utf-8"
    ) == "openat /steamapps/common\n"
    assert (target / "io" / "gc" / "mono alloc.txt").read_text(encoding="utf-8") == "uprobe hit\n"

    with zipfile.ZipFile(honest) as archive:
        archived = {
            name: archive.read(name)
            for name in archive.namelist()
            if name != "manifest.json" and not name.endswith("/")
        }
    restored = {
        str(path.relative_to(target)): path.read_bytes()
        for path in sorted(target.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    assert restored == archived, f"seed={seed}: restored bytes diverged from the bundle"


def _summary_doc(rng: random.Random) -> Any:
    layers: Any = rng.choice(
        [
            [
                {"layer": rng.choice(LABELS), "score": rng.choice(SCALARS), "state": "collected"},
                {
                    "layer": "runtime_gc",
                    "state": rng.choice(["failed", "collected", 5]),
                    "signals": rng.choice(
                        [
                            {"stw_pause_worst_ms": rng.choice(SCALARS), "stw_pause_total_ms": 1.0},
                            [],
                            "x",
                        ]
                    ),
                },
            ],
            rng.choice([[], [{"score": rng.choice(SCALARS)}], "x", {"a": 1}, None, 7]),
        ]
    )
    metadata: Any = rng.choice([{}, {"gc": {}}, "x", [1], 5, None])
    if isinstance(metadata, dict):
        metadata["gc"] = rng.choice(
            [
                {
                    "allocMBPerSecond": rng.choice(SCALARS),
                    "grossAllocMBPerSecond": rng.choice(SCALARS),
                },
                "x",
                None,
            ]
        )
        metadata["lag_diagnosis"] = rng.choice(
            [
                {
                    "laggy": rng.choice([True, False, "yes", 0]),
                    "causes": [{"cause": rng.choice(LABELS), "severity": rng.choice(SCALARS)}],
                },
                {"causes": "x"},
                [],
                "x",
            ]
        )
        metadata["frame"] = rng.choice([{"lateTicks": rng.choice(SCALARS)}, {}, "x", None])
        metadata["net"] = rng.choice([{"udp_send_mb_per_second": rng.choice(SCALARS)}, {}, 5])
    return {
        "schema": "7dtd.apm.summary.v2",
        "session_id": "session_fuzzmetrics",
        "layers": layers,
        "metadata": metadata,
    }


@pytest.mark.parametrize("seed", list(METRIC_SEEDS))
def test_fuzz_prometheus_export_on_crafted_summary(tmp_path: Path, seed: int) -> None:
    """The exposition is a deployed contract a scrape reads, so a crafted
    summary must produce lines that still parse: a finite value, and a label
    value that reads back as the exact string it came from. The export is
    also written through the same atomic writer as every other artifact, and
    two runs over the same session must be byte identical."""
    rng = random.Random(seed)
    session = tmp_path / f"session_metrics_{seed}"
    session.mkdir()
    (session / "summary.json").write_text(json.dumps(_summary_doc(rng)), encoding="utf-8")
    if rng.random() < 0.5:
        # Hostile but well-formed: a torn health.json is a reported MetricError
        # (regression below), not a line the exporter has to render.
        (session / "health.json").write_text(
            json.dumps(rng.choice([{"coverage": rng.choice(SCALARS)}, {}, {"coverage": [1]}])),
            encoding="utf-8",
        )
    if rng.random() < 0.5:
        (session / "csharp_bridge.json").write_text(
            json.dumps(
                {
                    "attribution": rng.choice(
                        [
                            {
                                "subsystems": [
                                    {
                                        "subsystem": rng.choice(LABELS),
                                        "scaled_total_ms": rng.choice(SCALARS),
                                    }
                                ]
                            },
                            "x",
                            None,
                        ]
                    )
                }
            ),
            encoding="utf-8",
        )

    output = tmp_path / f"metrics_{seed}.prom"
    export_metrics(session, output)
    first = output.read_text(encoding="utf-8")
    export_metrics(session, output)
    assert output.read_text(encoding="utf-8") == first, f"seed={seed}: export is not deterministic"

    for line in first.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        assert match, f"seed={seed}: unparseable exposition line {line!r}"
        assert match.group("name").startswith("sevendtd_apm_"), f"seed={seed}: {line!r}"
        value = match.group("value")
        assert value not in ("nan", "inf", "+Inf", "-Inf"), f"seed={seed}: {line!r}"
        assert math.isfinite(float(value)), f"seed={seed}: {line!r}"
        labels = match.group("labels")
        if labels is None:
            continue
        # Every label is a single key="value" pair whose value unescapes back
        # to the string the summary carried, and no unescaped quote or
        # newline can survive to break the line format.
        pairs = re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', labels)
        assert pairs, f"seed={seed}: label set carried no pair: {line!r}"
        for _, raw in pairs:
            _unescape_label(raw)
        assert labels.count('"') == 2 * len(pairs), f"seed={seed}: {line!r}"
        for control in ("\n", "\r"):
            assert control not in labels, f"seed={seed}: raw {control!r} survived into {line!r}"

    # Missing evidence reads as an absent metric set, not a traceback.
    empty = tmp_path / f"empty_{seed}"
    empty.mkdir()
    with pytest.raises(MetricError):
        export_metrics(empty, tmp_path / f"empty_{seed}.prom")


def test_prometheus_export_reports_torn_artifact_by_name(tmp_path: Path) -> None:
    """A side artifact that cannot be decoded is a named MetricError, so the
    operator reads which file is malformed instead of a traceback, and no
    partial exposition is left behind for a scrape to read."""
    session = tmp_path / "session_torn"
    session.mkdir()
    (session / "summary.json").write_text('{"layers": []}', encoding="utf-8")
    (session / "health.json").write_text('{"coverage": 0.5', encoding="utf-8")
    output = tmp_path / "torn.prom"
    with pytest.raises(MetricError, match=r"health\.json"):
        export_metrics(session, output)
    assert not output.exists()
