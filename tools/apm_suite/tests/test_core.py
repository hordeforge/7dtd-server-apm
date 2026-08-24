from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
import pytest
from apm_suite import capture, paths
from apm_suite.analysis.bridge import match_rules, parse_section_line
from apm_suite.analysis.events import PER_SOURCE_MAX, build_timeline
from apm_suite.analysis.health import build_health
from apm_suite.analysis.report import parse_perf_stat, top_alloc_sites
from apm_suite.capture import CaptureContext, CollectorSpec
from apm_suite.cli import app
from apm_suite.finalize import finalize
from apm_suite.io import atomic_json, load_json
from apm_suite.models import EventsV2, LayerScore, ManifestV2, Target, schema_dict
from apm_suite.reporting import render_session
from apm_suite.runner import terminate_tree
from apm_suite.session import REQUIRED, audit_session
from pydantic import ValidationError
from typer.testing import CliRunner

runner = CliRunner()
REPO = Path(__file__).parents[3]
FIXTURES = Path(__file__).with_name("fixtures")


def _meta(pid: int = 1, seconds: int = 10, only: str = "all") -> dict[str, object]:
    return {
        "schema": "7dtd.apm.session.v2",
        "utc": "2026-01-01T00:00:00Z",
        "pid": pid,
        "comm": "7DaysToDieServe",
        "seconds": seconds,
        "only": only,
        "no_app": False,
        "analyzer_version": "2.1.0",
    }


def _summary(session_id: str, layers: list[dict[str, object]]) -> dict[str, object]:
    return {"schema": "7dtd.apm.summary.v2", "session_id": session_id, "layers": layers}


def _events(session: str, events: list[dict[str, object]] | None = None) -> dict[str, object]:
    events = events or []
    return {
        "schema": "7dtd.apm.events.v2",
        "session": session,
        "count": len(events),
        "retained": len(events),
        "dropped": 0,
        "by_kind": {},
        "events": events,
    }


def _session(root: Path, only: str = "all") -> Path:
    root.mkdir()
    atomic_json(root / "meta.json", _meta(only=only))
    atomic_json(
        root / "summary.json",
        _summary(root.name, [{"layer": "cpu", "score": 10, "state": "collected"}]),
    )
    atomic_json(root / "events.json", _events(str(root)))
    atomic_json(
        root / "health.json",
        {"schema": "7dtd.apm.health.v2", "session": root.name, "confidence": "insufficient"},
    )
    for rel in REQUIRED:
        path = root / rel
        if not path.is_file():
            path.write_text("ok")
    return root


# --- unit: CLI + models -----------------------------------------------------


def test_cli_help_and_dry_run() -> None:
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["capture", "--seconds", "7", "--dry-run"])
    assert result.exit_code == 0
    assert "capture plan" in result.stdout
    assert "app/bridge.jsonl" in result.stdout


def test_cli_version_flag_works_without_subcommand() -> None:
    from apm_suite import __version__
    from typer.main import get_command

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
    # Every command exposes a one-line summary for the top-level help listing.
    commands = get_command(app).commands  # type: ignore[attr-defined]
    for name in (
        "doctor",
        "capture",
        "finalize",
        "audit",
        "index",
        "export",
        "import",
        "scaling",
        "prometheus",
        "monitor",
        "prune",
        "compare",
        "budget",
        "bridge",
        "flame",
        "scenario",
    ):
        # click leaves short_help unset and renders the docstring's first line.
        summary = commands[name].short_help or (commands[name].help or "").splitlines()[0]
        assert summary.strip(), f"missing short help for {name}"


def test_cli_usage_errors_exit_2_on_stderr_never_stdout(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    cases: list[list[str]] = [
        ["finalize", str(missing)],
        ["audit", str(missing)],
        ["bridge", str(missing)],
        ["budget", str(missing)],
        ["compare", str(missing), str(tmp_path / "nope2")],
        ["scaling", "--by", "bots", "a", "b", "c"],
        ["monitor"],
    ]
    for argv in cases:
        result = runner.invoke(app, argv)
        assert result.exit_code == 2, argv
        assert result.stdout == "", f"error leaked to stdout: {argv}"
        assert result.stderr.strip(), f"no stderr diagnostics: {argv}"
        assert "[red]" not in result.stderr, f"raw markup leaked: {argv}"


def test_capture_rejects_unknown_only_tokens_even_dry_run() -> None:
    bad = runner.invoke(app, ["capture", "--only", "cpu,memry", "--dry-run"])
    assert bad.exit_code == 2
    assert "memry" in bad.stderr and "cpu" not in bad.stderr.split("unknown")[1]
    for good in ("all", "alloc", "app_sim,futex", "cpu , memory", "net", ""):
        assert runner.invoke(app, ["capture", "--only", good, "--dry-run"]).exit_code == 0


def test_budget_rejects_missing_budget_file_before_running(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "summary.json").write_text("{}")
    result = runner.invoke(app, ["budget", str(session), "--budget", str(tmp_path / "b.json")])
    assert result.exit_code == 2
    assert "budget file not found" in result.stderr


def test_budget_rejects_unparseable_budget_file_cleanly(tmp_path: Path) -> None:
    """A torn or hand-mangled budget JSON must fail with a named-path error,
    not a traceback, and must never silently run against DEFAULT_BUDGET."""
    from apm_suite.analysis.budget import check_budget

    session = tmp_path / "session"
    session.mkdir()
    (session / "summary.json").write_text("{}")
    bad = tmp_path / "budget.json"
    bad.write_text('{"max_layer_scores": ')
    result = runner.invoke(app, ["budget", str(session), "--budget", str(bad)])
    assert result.exit_code == 2
    # Rich wraps stderr at console width (even mid-word); compare squashed.
    squashed = _squashed(result.stderr)
    assert str(bad) in squashed
    assert "notvalidJSON" in squashed
    with pytest.raises(ValueError, match="not valid JSON"):
        check_budget(session, bad)


def test_budget_rejects_non_object_budget_file(tmp_path: Path) -> None:
    from apm_suite.analysis.budget import check_budget

    session = tmp_path / "session"
    session.mkdir()
    (session / "summary.json").write_text("{}")
    bad = tmp_path / "budget.json"
    bad.write_text("[1, 2, 3]")
    with pytest.raises(ValueError, match="JSON object"):
        check_budget(session, bad)


def test_thread_summary_skips_torn_final_line(tmp_path: Path) -> None:
    """A collector killed at the window deadline can leave a truncated last
    jsonl line; the required summary stage must use the intact samples instead
    of crashing on the torn one."""
    from apm_suite.analysis.report import thread_summary

    session = tmp_path / "session_torn"
    (session / "threads").mkdir(parents=True)
    good = json.dumps({"t": 1.0, "n_threads": 4, "states": {"S": 4}, "wchan_top": {}, "top": []})
    (session / "threads/threads.jsonl").write_text(good + "\n" + good[:20])
    summary = thread_summary(session)
    assert summary["n_threads"] == 4


def test_thread_summary_survives_junk_values_and_finds_string_tid(
    tmp_path: Path,
) -> None:
    """threads.jsonl is re-read without schema guarantees: a valid-JSON row with
    a non-numeric cpu_pct must degrade to "no contribution", not crash the
    required summary stage, and a float/string tid (older writer, hand edit)
    must still match the main pid instead of silently reporting the main thread
    at 0% share."""
    from apm_suite.analysis.report import thread_summary

    session = tmp_path / "session_junk_rows"
    (session / "threads").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta(pid=5))
    row = json.dumps(
        {
            "t": 1.0,
            "top": [
                {"tid": 5, "cpu_pct": 30.0},
                {"tid": 6.0, "cpu_pct": "90.0"},  # coercible string tid/pct still count
                {"tid": 7, "cpu_pct": {"corrupt": True}},  # junk pct drops out
            ],
        }
    )
    (session / "threads/threads.jsonl").write_text(row + "\n")
    summary = thread_summary(session)
    assert summary["main_thread_cpu_pct_avg"] == 30.0
    # main 30 of process total 120
    assert summary["main_thread_share_of_process_avg"] == pytest.approx(0.25)


def test_bridge_spikes_with_corrupt_duration_do_not_kill_timeline(
    tmp_path: Path,
) -> None:
    """spikes[] sits outside BridgeSnapshotV3 validation (extra="allow"), so a
    format-changed duration field must coerce to "no data" like every other
    collector field instead of raising out of the required events stage; the
    intact sibling spike is still recorded."""
    from apm_suite.analysis.events import build_timeline

    session = tmp_path / "session_corrupt_spike"
    (session / "app").mkdir(parents=True)
    atomic_json(
        session / "app/apm_app.json",
        {
            "spikes": [
                {"utc": "2026-07-16T10:00:00Z", "gmUpdateDurationMs": {"corrupt": True}},
                {
                    "utc": "2026-07-16T10:00:01Z",
                    "gmUpdateDurationMs": 250.0,
                    "serverTickIntervalMs": None,
                },
            ]
        },
    )
    doc = build_timeline(session)
    spikes = [e for e in doc.events if e.kind == "frame_spike"]
    assert len(spikes) == 2
    intact = next(e for e in spikes if e.model_dump(mode="json")["value"] == 250.0)
    assert "tickInterval 0.0ms" in intact.message


def test_index_scan_survives_non_numeric_layer_score(tmp_path: Path) -> None:
    """A tampered/imported summary.json whose layer score is not numeric must
    drop out of sum_pressure instead of poisoning the whole store index scan
    (one bad session would otherwise brick `index` and every dashboard)."""
    from apm_suite.analysis.index import scan

    bad = tmp_path / "session_bad"
    bad.mkdir()
    (bad / "summary.json").write_text(
        json.dumps(
            {
                "schema": "7dtd.apm.summary.v2",
                "layers": [
                    {"layer": "cpu", "state": "collected", "score": "12abc"},
                    {"layer": "io", "state": "collected", "score": 40},
                ],
            }
        )
    )
    rows = scan(tmp_path)
    assert len(rows) == 1
    assert rows[0]["sum_pressure"] == 40.0


def test_scenario_matrix_rejects_missing_plan_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scenario", "matrix", str(tmp_path / "plan.json")])
    assert result.exit_code == 2
    assert "plan file not found" in result.stderr


# --- unit: checkout backends guard ------------------------------------------


def test_require_backends_passes_in_checkout() -> None:
    # The repository tree always carries tools/apm/collectors.
    paths.require_backends()


def test_require_backends_fails_without_backend_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An installed-wheel copy ships only apm_suite; the guard must fail loudly
    # instead of letting every collector die with file-not-found noise.
    monkeypatch.setattr(paths, "APM_BACKENDS", tmp_path / "tools" / "apm")
    with pytest.raises(RuntimeError, match="collector backends missing"):
        paths.require_backends()
    with pytest.raises(RuntimeError, match="collector backends missing"):
        capture.run_capture(
            seconds=1,
            pid=1,
            only="all",
            no_app=False,
            telnet_host="",
            telnet_port=0,
            telnet_password="",
        )


def test_flame_build_reports_missing_backends_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "APM_BACKENDS", tmp_path / "tools" / "apm")
    result = runner.invoke(app, ["flame", "build", str(tmp_path)])
    assert result.exit_code == 2
    assert "collector backends missing" in result.stderr


def test_doctor_json_stdout_is_machine_readable() -> None:
    result = runner.invoke(app, ["doctor", "--json", "-"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["schema"].startswith("7dtd.apm.doctor")


def test_alloc_sites_rank_by_bytes_skip_noise(tmp_path: Path) -> None:
    # bpftrace prints maps ASCENDING, so the heaviest stack is last; the site is
    # the first game frame under GC_malloc, past BCL/profiler/hex noise.
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "mono_alloc.bt.annotated.txt").write_text(
        "=== top large-allocation sites by bytes (top 20) ===\n"
        "@big_alloc_bytes[\n"
        "        GC_malloc+0\n"
        "        GameTimer.Reset+0x3c\n"
        "]: 61272\n"
        "@big_alloc_bytes[\n"
        "        GC_malloc+0\n"
        "        Unity.Profiling.Memory.MemoryProfiler.add_x+0x1\n"
        "        System.String.SplitInternal+0x2\n"
        "        UnityEngine.Quaternion.FromToRotation+0x4\n"  # engine leaf, skip to game caller
        "        NetEntityDistribution.OnUpdateEntities+0x9\n"
        "]: 500000\n"
        "@big_alloc_bytes[\n"
        "        GC_malloc+0\n"
        "        0x7f39b0846428\n"
        "        AstarVoxelGrid.InitScan+0xc0e\n"
        "]: 9961728\n"
        "\n=== top large-allocation sites by count (top 20) ===\n"
        "@big_alloc_count[\n        GC_malloc+0\n        DoNotPick.Me+0x1\n]: 3\n"
    )
    sites = top_alloc_sites(tmp_path, limit=3)
    # heaviest first; profiler/BCL/hex skipped; the count section is not read.
    assert sites == [
        "AstarVoxelGrid.InitScan",
        "NetEntityDistribution.OnUpdateEntities",
        "GameTimer.Reset",
    ]
    assert "DoNotPick.Me" not in sites


def test_gc_slow_collect_not_double_counted() -> None:
    from apm_suite.analysis.report import _gc_layer

    # Two real slow collects. "SLOW mono_gc" is a prefix of "SLOW mono_gc_collect",
    # so the old code counted each line twice (slow_gc=4).
    text = "SLOW mono_gc_collect 5000 us\nSLOW mono_gc_collect 6000 us\n"
    layer = _gc_layer({"mono_gc": text})
    assert layer.signals["slow_gc_lines"] == 2


def test_bt_cumulative_counter_takes_last(tmp_path: Path) -> None:
    from apm_suite.analysis.events import EventSink, parse_bt_slow

    # @wait_n is a non-reset cumulative printed each interval; the total is the LAST.
    p = tmp_path / "futex.bt.out"
    p.write_text("@wait_n: 3\n@wait_n: 9\n@wait_n: 40\n")
    sink = EventSink()
    parse_bt_slow(sink, p, "futex")
    counters = [e for e in sink.events if e["kind"] == "counter" and "futex_waits" in e["message"]]
    assert counters and counters[0]["value"] == 40


def test_cpu_hot_paths_attributes_to_game_frame(tmp_path: Path) -> None:
    from apm_suite.analysis.report import top_cpu_hot_paths

    perf = tmp_path / "cpu/perf"
    perf.mkdir(parents=True)
    # folded: frame;frame;...;leaf  count. Native/BCL leaves must attribute down to
    # the first game frame; inclusive keeps everything.
    (perf / "stacks.folded").write_text(
        "GameManager.Update;EntityAlive.updateTasks;GC_dirty_inner 100\n"
        "GameManager.Update;NetConnectionSimple.taskSerialize;[libc.so.6] 60\n"
        "[UnityPlayer.so] 40\n"
    )
    # main-thread view: only the sim thread's stacks (tid==pid)
    (perf / "stacks.main.folded").write_text(
        "GameManager.Update;World.TickEntities;EntityAlive.Update 30\n"
    )
    r = top_cpu_hot_paths(tmp_path, limit=5)
    self_names = [n for n, _ in r["self_game"]]
    # leaf GC_dirty_inner -> updateTasks (skip native); libc -> taskSerialize
    assert "EntityAlive.updateTasks" in self_names
    assert "NetConnectionSimple.taskSerialize" in self_names
    assert "GC_dirty_inner" not in self_names and "[libc.so.6]" not in self_names
    # inclusive keeps native + game; percentages of 200 total samples
    incl = dict(r["inclusive"])
    assert incl["GameManager.Update"] == 80.0  # (100+60)/200
    # main_thread comes from the separate main.folded (leaf -> EntityAlive.Update)
    assert r["main_thread"] and r["main_thread"][0][0] == "EntityAlive.Update"


def test_export_scrub_redacts_nested_cmdline_exe() -> None:
    from apm_suite.cli import _scrub

    data = {"meta": {"cmdline": "-configfile=/secret", "exe": "/opt/7dtd", "pid": 42}}
    out = _scrub(data)
    assert out == {
        "meta": {"cmdline": "<redacted>", "exe": "<redacted>", "pid": 42}
    }  # nested redaction; non-sensitive fields preserved


def test_export_bundle_scrubs_jsonl_and_path_bearing_text(tmp_path: Path) -> None:
    import zipfile

    home = str(Path.home())
    session = tmp_path / "session_export"
    (session / "io").mkdir(parents=True)
    (session / "cpu/perf").mkdir(parents=True)
    (session / "app").mkdir()
    atomic_json(session / "meta.json", _meta())
    (session / "events.jsonl").write_text(
        json.dumps({"t": 1.0, "cmdline": "-quiet", "message": f"open {home}/save"}) + "\n"
        f"truncated-line {home}/more\n"
    )
    (session / "io/vfs.bt.out").write_text(f"openat {home}/steamapps/common\n")
    (session / "cpu/perf/flame.svg").write_text(f"<title>frame {home}/libgame.so</title>\n")
    # bridge.jsonl is raw telnet evidence and must stay out of bundles entirely.
    (session / "app/bridge.jsonl").write_text("Player 'Alice' joined from 203.0.113.7\n")

    bundle = tmp_path / "bundle.zip"
    result = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        events_line = archive.read("events.jsonl").decode()
        vfs = archive.read("io/vfs.bt.out").decode()
        svg = archive.read("cpu/perf/flame.svg").decode()
    assert home not in events_line + vfs + svg
    assert '"cmdline": "<redacted>"' in events_line
    assert "~/save" in events_line and "truncated-line ~/more" in events_line
    assert f"openat {home}" not in vfs and "openat ~/steamapps/common" in vfs
    assert "~/libgame.so" in svg
    assert "app/bridge.jsonl" not in names


def test_import_bundle_round_trip_restores_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Export must have a working inverse: a sanitized bundle restores back into
    the store as an auditable session (the restore path is proven, not assumed)."""
    session = tmp_path / "session_roundtrip"
    (session / "io").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta())
    (session / "events.jsonl").write_text('{"t": 1.0, "message": "tick"}\n')
    (session / "io/vfs.bt.out").write_text("openat /steamapps/common\n")

    bundle = tmp_path / "session_roundtrip.zip"
    exported = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert exported.exit_code == 0, exported.output

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(store))
    result = runner.invoke(app, ["import", str(bundle)])
    assert result.exit_code == 0, result.output

    restored = store / "session_roundtrip"
    assert (restored / "meta.json").is_file()
    assert (restored / "events.jsonl").read_text() == '{"t": 1.0, "message": "tick"}\n'
    assert (restored / "io/vfs.bt.out").read_text() == "openat /steamapps/common\n"
    # audit_session ran during import and recorded the integrity manifest.
    assert (restored / "manifest.json").is_file()
    assert load_json(restored / "manifest.json")["session_id"] == "session_roundtrip"


def test_import_bundle_without_session_prefix_lands_in_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zipfile

    bundle = tmp_path / "evidence.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("meta.json", "{}\n")

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(store))
    result = runner.invoke(app, ["import", str(bundle)])
    assert result.exit_code == 0, result.output
    assert (store / "session_evidence").is_dir()


def test_import_normalizes_nfd_bundle_stem_to_nfc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A macOS NFD bundle filename and its NFC spelling must claim the same
    session directory name: identity is NFC at ingestion, not byte-equal."""
    import unicodedata
    import zipfile

    nfd = unicodedata.normalize("NFD", "café")
    assert nfd != unicodedata.normalize("NFC", "café")
    bundle = tmp_path / f"session_{nfd}.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("meta.json", "{}\n")

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(store))
    result = runner.invoke(app, ["import", str(bundle)])
    assert result.exit_code == 0, result.output
    assert (store / "session_café").is_dir()
    assert list(store.iterdir()) == [store / "session_café"]


def test_export_round_trips_non_ascii_evidence_bytes(
    tmp_path: Path,
) -> None:
    """Sanitized bundles must carry non-ASCII evidence through as UTF-8 bytes,
    independent of the host locale the export ran under."""
    import zipfile

    session = tmp_path / "session_unicode"
    (session / "io").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta())
    note = "player ☃ joined — NFD: café"
    (session / "io/vfs.bt.out").write_text(note + "\n", encoding="utf-8")
    atomic_json(session / "summary.json", _summary("session_unicode", []))

    bundle = tmp_path / "session_unicode.zip"
    exported = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert exported.exit_code == 0, exported.output

    with zipfile.ZipFile(bundle) as archive:
        raw = archive.read("io/vfs.bt.out")
        summary = json.loads(archive.read("summary.json").decode("utf-8"))
    assert raw.decode("utf-8") == note + "\n"
    assert summary["session_id"] == "session_unicode"


def test_import_bundle_twice_keeps_runs_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running an import (double-click, script retry) must not merge the
    second bundle into the first restore target: each run claims its own
    directory and the first copy stays byte-identical."""
    session = tmp_path / "session_retry"
    (session / "io").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta())
    (session / "io/vfs.bt.out").write_text("openat /steamapps/common\n")

    bundle = tmp_path / "session_retry.zip"
    exported = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert exported.exit_code == 0, exported.output

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(store))
    first = runner.invoke(app, ["import", str(bundle)])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["import", str(bundle)])
    assert second.exit_code == 0, second.output

    original = store / "session_retry"
    duplicate = store / "session_retry_1"
    assert original.is_dir() and duplicate.is_dir()
    # First copy untouched by the rerun.
    assert (original / "io/vfs.bt.out").read_text() == "openat /steamapps/common\n"
    assert load_json(original / "manifest.json")["artifacts"]
    assert (duplicate / "meta.json").is_file()


def test_import_rejects_zip_slip_and_corrupt_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted bundle must never write outside the restore target."""
    import zipfile

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(store))

    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../escape.txt", "nope")
        archive.writestr("/abs.txt", "nope")
    result = runner.invoke(app, ["import", str(evil)])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert not (tmp_path / "escape.txt").exists()
    assert not list(store.glob("session_*"))

    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip file")
    result = runner.invoke(app, ["import", str(corrupt)])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert not list(store.glob("session_*"))


def test_import_rejects_bundles_beyond_size_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decompression-bomb bundle (huge declared uncompressed size or member
    count) is refused before any extraction touches the session store."""
    from apm_suite import cli as cli_module

    session = tmp_path / "session_bomb"
    (session / "io").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta())
    (session / "io/vfs.bt.out").write_text("openat /steamapps/common\n")
    bundle = tmp_path / "session_bomb.zip"
    exported = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert exported.exit_code == 0, exported.output

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(store))
    monkeypatch.setattr(cli_module, "MAX_IMPORT_UNCOMPRESSED_BYTES", 4)
    result = runner.invoke(app, ["import", str(bundle)])
    assert result.exit_code == 2
    assert "import limits" in result.output
    assert not list(store.glob("session_*"))

    monkeypatch.setattr(cli_module, "MAX_IMPORT_UNCOMPRESSED_BYTES", 2**40)
    monkeypatch.setattr(cli_module, "MAX_IMPORT_MEMBERS", 1)
    result = runner.invoke(app, ["import", str(bundle)])
    assert result.exit_code == 2
    assert "import limits" in result.output
    assert not list(store.glob("session_*"))


def test_as_number_rejects_non_finite_and_boolean_scalars() -> None:
    from apm_suite.models import as_number

    assert as_number(42) == 42.0
    assert as_number("3.5") == 3.5
    assert as_number(True) is None
    assert as_number(False) is None
    assert as_number(None) is None
    assert as_number("abc") is None
    assert as_number(float("inf")) is None
    assert as_number(float("nan")) is None
    # JSON 1e999 parses to inf through the stdlib reader.
    assert as_number(json.loads("1e999")) is None


def test_collected_layer_scores_skips_unparseable_scores() -> None:
    """Summary JSON is unvalidated on read paths; a junk score must degrade to
    "no evidence for that layer" instead of raising mid-analysis."""
    from apm_suite.models import collected_layer_scores

    summary = {
        "layers": [
            {"layer": "cpu", "state": "collected", "score": 10},
            {"layer": "runtime_gc", "state": "collected", "score": "garbage"},
            {"layer": "io", "state": "skipped", "score": 50},
        ]
    }
    assert collected_layer_scores(summary) == {"cpu": 10.0}


def test_prometheus_drops_malformed_metric_fields_instead_of_crashing(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path / "session_junk")
    summary = load_json(session / "summary.json")
    summary["layers"].append({"layer": "runtime_gc", "state": "collected", "score": "garbage"})
    summary["metadata"] = {
        "frame": {"lateTicks": {"boom": 1}},
        "gc": {"allocMBPerSecond": "not-a-number", "grossAllocMBPerSecond": None},
        "net": {"udp_send_mb_per_second": json.loads("1e999")},
        "lag_diagnosis": {
            "laggy": True,
            "causes": [{"cause": "gc_pauses", "severity": "high"}],
        },
    }
    atomic_json(session / "summary.json", summary)
    out = tmp_path / "metrics.txt"
    result = runner.invoke(app, ["prometheus", str(session), "--output", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text()
    # The valid numeric layer still exports; every malformed field drops its line.
    assert 'sevendtd_apm_layer_pressure{layer="cpu"}' in text
    assert "runtime_gc" not in text
    assert "late_ticks" not in text
    assert "alloc_mb_per_second" not in text
    assert "udp_send_mb_per_second" not in text
    # A non-numeric cause severity falls back to 0 rather than crashing.
    assert 'sevendtd_apm_lag_cause_severity{cause="gc_pauses"} 0.000' in text
    assert "Infinity" not in text and "inf" not in text.replace("inflate", "")


def test_budget_fails_closed_on_unparseable_summary_numbers(tmp_path: Path) -> None:
    """Unparseable gate inputs are UNKNOWN (gate fails); they must neither pass
    silently nor raise a conversion traceback."""
    session = _session(tmp_path / "session_gate")
    summary = load_json(session / "summary.json")
    summary["metadata"] = {
        "gc": {"grossAllocMBPerSecond": "12x"},
        "net": {"udp_send_mb_per_second": []},
        "frame": {"lateTicks": "many", "windowUpdates": 1000},
    }
    atomic_json(session / "summary.json", summary)
    result = runner.invoke(app, ["budget", str(session)])
    assert result.exit_code == 1
    assert "UNKNOWN max_gross_alloc_mb_per_second" in result.output
    assert "UNKNOWN max_udp_send_mb_per_second" in result.output
    assert "UNKNOWN late_ticks" in result.output
    assert "Traceback" not in result.output


def test_compare_tolerates_malformed_numbers_in_both_sessions(tmp_path: Path) -> None:
    def make(name: str) -> Path:
        session = _session(tmp_path / name)
        summary = load_json(session / "summary.json")
        summary["meta"] = {"analyzer_version": "2.1.0", "only": "all", "seconds": 60}
        summary["metadata"] = {
            "frame": {"lateTicks": "many"},
            "gc": {"grossAllocMBPerSecond": "junk"},
            "transfers": {"mb_per_second": []},
        }
        summary["layers"].append(
            {
                "layer": "runtime_gc",
                "state": "collected",
                "score": 5,
                "signals": {"stw_pause_worst_ms": "junk"},
            }
        )
        atomic_json(session / "summary.json", summary)
        atomic_json(
            session / "csharp_bridge.json",
            {
                "schema": "7dtd.apm.bridge.v2",
                "attribution": {
                    "subsystems": [{"subsystem": "network", "scaled_total_ms": "junk"}]
                },
                "top_managed_sections": [{"name": "World.TickEntities", "score": "junk"}],
            },
        )
        return session

    before, after = make("session_before"), make("session_after")
    result = runner.invoke(app, ["compare", str(before), str(after)])
    assert result.exit_code == 0, result.output
    cmp_doc = load_json(after / "compare.json")
    assert cmp_doc["late_ticks_a"] == 0 and cmp_doc["alloc_mb_s_a"] == 0.0
    assert cmp_doc["stw_worst_ms_a"] == 0.0
    # Junk section/attribution heat degrades to 0.0 ties, never a bogus winner
    # and never a conversion crash.
    for delta in cmp_doc["section_deltas"] + cmp_doc["attribution_deltas"]:
        assert delta["a_heat" if "a_heat" in delta else "a_ms"] == 0.0
        assert delta["better"] in ("tie", "not_comparable")


def test_models_emit_v2_schema() -> None:
    model = ManifestV2(
        session_id="session_test",
        started_at=datetime.now(UTC),
        target=Target(pid=1),
        requested_layers=["all"],
    )
    assert schema_dict(model)["schema"] == "7dtd.apm.manifest.v2"


def test_atomic_json_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested/value.json"
    atomic_json(path, {"snowman": "☃"})
    assert json.loads(path.read_text()) == {"snowman": "☃"}


def test_atomic_write_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each atomic write must fsync the file data AND the parent directory: a
    rename without a directory fsync can be lost on power failure, silently
    reverting evidence files that later audits hash."""
    calls: list[int] = []
    real_fsync = os.fsync

    def counting_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)

    path = tmp_path / "nested/value.json"
    atomic_json(path, {"a": 1})
    atomic_json(path, {"a": 2})

    assert len(calls) >= 4  # two writes x (file fsync + directory fsync)
    assert json.loads(path.read_text()) == {"a": 2}


def test_events_schema_enforces_count_identities() -> None:
    """CHECK-constraint analog at the ingestion boundary: count = retained +
    dropped and retained = len(events) must hold or validation fails, so a
    corrupt/hand-edited events.json cannot feed readers inconsistent totals."""
    base = {
        "schema": "7dtd.apm.events.v2",
        "session": "session_x",
        "count": 3,
        "retained": 2,
        "dropped": 1,
        "by_kind": {},
        "events": [
            {"kind": "gc", "severity": "info", "message": "a"},
            {"kind": "gc", "severity": "warn", "message": "b"},
        ],
    }
    EventsV2.model_validate(base)

    mismatched_total = dict(base, count=4)
    with pytest.raises(ValidationError, match="count=4"):
        EventsV2.model_validate(mismatched_total)

    mismatched_retained = dict(base, retained=1)
    with pytest.raises(ValidationError, match="retained=1"):
        EventsV2.model_validate(mismatched_retained)


def test_password_not_in_capture_command() -> None:
    result = runner.invoke(app, ["capture", "--telnet-password", "super-secret", "--dry-run"])
    assert result.exit_code == 0
    assert "super-secret" not in result.stdout


# --- unit: audit + collector result ingestion --------------------------------


def test_audit_hashes_all_artifacts_without_touching_fixture(tmp_path: Path) -> None:
    session = _session(tmp_path / "session_test")
    (session / "extra.txt").write_text("evidence")
    manifest, valid = audit_session(session)
    assert valid
    assert any(item.path == "extra.txt" for item in manifest.artifacts)
    assert load_json(session / "manifest.json")["schema"] == "7dtd.apm.manifest.v2"


def test_audit_detects_tampered_artifact_and_preserves_baseline(tmp_path: Path) -> None:
    """`audit` must verify against the RECORDED hashes (its documented contract),
    not re-stamp current contents: edited evidence fails, and the failed audit
    leaves the original manifest in place so the drift stays provable."""
    session = _session(tmp_path / "session_tamper")
    assert audit_session(session)[1]
    baseline = load_json(session / "manifest.json")
    (session / "summary.json").write_text('{"tampered": true}\n')
    result = runner.invoke(app, ["audit", str(session)])
    assert result.exit_code == 1
    assert "INVALID" in result.stdout
    # The offending path is named on stderr, not just counted.
    assert "summary.json" in result.stderr
    # The recorded baseline survives the failed verification.
    assert load_json(session / "manifest.json") == baseline


def test_audit_accepts_newly_attached_artifacts(tmp_path: Path) -> None:
    """Attaching an extra artifact then re-auditing stays valid (docs/APM.md):
    only changes to already-recorded artifacts are integrity failures."""
    session = _session(tmp_path / "session_attach")
    assert audit_session(session)[1]
    (session / "extra.txt").write_text("attached after the first audit")
    result = runner.invoke(app, ["audit", str(session)])
    assert result.exit_code == 0
    assert "valid" in result.stdout


def test_bridge_export_period_from_config_or_default(tmp_path: Path) -> None:
    """The monitor's stale-read threshold keys off the bridge's export cadence
    (PeriodicExportSeconds), falling back to 30 on absent/invalid config; a
    non-positive value must not collapse the threshold to zero."""
    from apm_suite.cli import bridge_export_period

    telemetry = tmp_path / "telemetry"
    telemetry.mkdir()
    assert bridge_export_period(telemetry) == 30.0  # no config file
    config = telemetry.parent / "Config"
    config.mkdir()
    atomic_json(config / "apmbridge.json", {"PeriodicExportSeconds": 60})
    assert bridge_export_period(telemetry) == 60.0
    atomic_json(config / "apmbridge.json", {"PeriodicExportSeconds": 0})
    assert bridge_export_period(telemetry) == 30.0
    (config / "apmbridge.json").write_text("not json\n")
    assert bridge_export_period(telemetry) == 30.0


def _result_json(status: str, name: str = "futex", layer: str = "sync_locks") -> dict[str, object]:
    return {
        "schema": "7dtd.apm.collector-result.v1",
        "name": name,
        "layer": layer,
        "status": status,
        "exit_code": 1 if status in ("failed", "interrupted") else 127,
        "duration_seconds": 3.5,
        "tool": "bpftrace",
        "tool_version": "bpftrace v0.21.0",
        "sample_count": 0,
        "artifacts": [],
        "message": f"fixture {status} capture",
    }


@pytest.mark.parametrize("status", ["failed", "unavailable", "interrupted"])
def test_audit_reports_structured_collector_failures(tmp_path: Path, status: str) -> None:
    session = _session(tmp_path / f"session_{status}")
    (session / "sync").mkdir()
    atomic_json(session / "sync/futex.result.json", _result_json(status))
    manifest, valid = audit_session(session)
    assert valid  # collector failure is a warning, not an integrity error
    futex = next(c for c in manifest.collectors if c.name == "futex")
    assert futex.status == status
    assert futex.tool_version == "bpftrace v0.21.0"
    assert futex.duration_seconds == 3.5
    assert any("futex" in w for w in manifest.warnings)


def test_audit_rejects_malformed_collector_result(tmp_path: Path) -> None:
    session = _session(tmp_path / "session_bad_result")
    (session / "sync").mkdir()
    atomic_json(session / "sync/futex.result.json", {"name": "futex", "status": "nonsense"})
    manifest, valid = audit_session(session)
    assert not valid
    assert any("invalid collector result" in e for e in manifest.errors)


def test_audit_rejects_summary_failing_schema_validation(tmp_path: Path) -> None:
    session = _session(tmp_path / "session_bad_summary")
    atomic_json(session / "summary.json", {"schema": "7dtd.apm.summary.v2", "layers": "nope"})
    manifest, valid = audit_session(session)
    assert not valid
    assert any("summary.json" in e for e in manifest.errors)


def test_audit_survives_malformed_meta_types(tmp_path: Path) -> None:
    # audit must report a bad session, not crash: a non-numeric pid and a
    # non-string `only` (both possible from hand-edited/corrupt meta.json) must
    # fall back to defaults instead of raising.
    session = _session(tmp_path / "session_bad_meta")
    atomic_json(session / "meta.json", {"pid": "not-a-number", "only": [1, 2], "utc": "garbage"})
    manifest, valid = audit_session(session)
    assert manifest.target.pid == 1
    assert manifest.requested_layers == ["all"]


# --- parser fixtures ----------------------------------------------------------


def test_parse_perf_stat_fixture() -> None:
    parsed = parse_perf_stat((FIXTURES / "hw_stat.txt").read_text())
    assert parsed["cycles"] == 123456789
    assert parsed["instructions"] == 98765432
    assert parsed["time_elapsed_s"] == pytest.approx(10.01)


def test_parse_managed_section_line_fixture() -> None:
    sections = parse_section_line("TickEntities=12.50ms(x400,max=99.1) noise text")
    assert sections == [{"name": "TickEntities", "avgMs": 12.5, "calls": 400, "maxMs": 99.1}]


def test_events_bound_materialization_but_count_everything(tmp_path: Path) -> None:
    session = tmp_path / "session_events"
    (session / "sync").mkdir(parents=True)
    (session / "sync/futex.bt.out").write_text(
        "\n".join(f"SLOW_FUTEX tid=1 wait={i}ms" for i in range(600))
    )
    doc = build_timeline(session)
    assert doc.count == 600
    assert len(doc.events) == PER_SOURCE_MAX
    assert doc.by_kind["futex"] == 600
    assert doc.dropped == 600 - PER_SOURCE_MAX


def test_stackcollapse_keeps_module_for_unknown_frames() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "stackcollapse_perf", REPO / "tools/host_profiler/stackcollapse_perf.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    perf_script = io.StringIO(
        "srv 1/1 [000] 1.0: cycles:\n"
        "\t7f01 [unknown] (/usr/lib/libmonobdwgc-2.0.so)\n"
        "\t7f02 GameManager.gmUpdate+0x42 (/tmp/perf-1.map)\n"
        "\t7f03 [unknown] ([unknown])\n"
        "\n"
    )
    counts = module.collapse(perf_script)
    assert counts == {"[jit];GameManager.gmUpdate;[libmonobdwgc-2.0.so]": 1}


def test_bridge_rules_require_thresholded_evidence() -> None:
    quiet = match_rules(
        frames=[("harmless_frame", 997), ("futex_wait", 3)],
        top_sections=[],
        collected_layers={"sync_locks"},
        layer_signals={"sync_locks": {"slow_futex_lines": 0}},
    )
    assert not quiet
    loud = match_rules(
        frames=[("harmless_frame", 900), ("futex_wait", 100)],
        top_sections=[],
        collected_layers={"sync_locks"},
        layer_signals={"sync_locks": {"slow_futex_lines": 12}},
    )
    assert loud
    hit = loud[0]
    assert set(hit) >= {"evidence", "derived", "inference", "experiment"}
    assert hit["evidence"]["layer_signals"] == {"slow_futex_lines": 12.0}
    assert hit["derived"]["native_weight_share"] == pytest.approx(0.1)
    assert hit["experiment"]["harmony_targets"]


def test_attribution_scales_deep_sections_and_excludes_long_running() -> None:
    from apm_suite.analysis.bridge import attribute_subsystems

    sections = [
        {"name": "EntityMoveHelper.UpdateMoveHelper", "totalMs": 100.0, "calls": 50, "deep": True},
        {"name": "DecoManager.UpdateTick", "totalMs": 400.0, "calls": 2000, "deep": False},
        {
            "name": "NetConnectionSimple.taskSerialize",
            "totalMs": 9e6,
            "avgMs": 33000.0,
            "calls": 200,
        },
    ]
    result = attribute_subsystems(sections, deep_sample_rate=16, window_updates=2000, entities=100)
    by_name = {s["subsystem"]: s for s in result["subsystems"]}
    # movement is nested inside the entity chain: drill-down only, never additive
    assert "movement" not in by_name
    drill = {d["level"]: d["scaled_total_ms"] for d in result["entity_drilldown"]["levels"]}
    assert drill["movement"] == 1600.0  # 100 * 16
    assert by_name["deco_world"]["scaled_total_ms"] == 400.0
    assert result["long_running_excluded"] == ["NetConnectionSimple.taskSerialize"]
    assert result["measured_ms"] == 400.0  # additive buckets only
    assert result["ms_per_tick"] == 0.2
    assert result["ms_per_entity_tick"] == 0.002


def test_attribution_shares_sum_to_one_without_double_counting_frame_core() -> None:
    from apm_suite.analysis.bridge import attribute_subsystems

    # frame_core (GameManager.UpdateTick) is INCLUSIVE of io_saves + entity_tick;
    # its exclusive time must be used in the denominator so shares are not
    # deflated by double-counting the nested buckets.
    sections = [
        {"name": "GameManager.UpdateTick", "totalMs": 1000.0, "calls": 100, "deep": False},
        {"name": "World.SaveWorldState", "totalMs": 300.0, "calls": 10, "deep": False},
        {"name": "World.TickEntity", "totalMs": 200.0, "calls": 100, "deep": True},
        {"name": "NetConnectionSimple.taskSerialize", "totalMs": 500.0, "calls": 50, "deep": False},
    ]
    result = attribute_subsystems(sections, deep_sample_rate=1, window_updates=100, entities=50)
    total_share = sum(s["share"] for s in result["subsystems"])
    assert total_share == pytest.approx(1.0, abs=0.01)  # no double-count in denominator
    by_name = {s["subsystem"]: s for s in result["subsystems"]}
    # frame_core is reported EXCLUSIVE (< its inclusive 1000ms): at least the
    # io_saves bucket (300) is subtracted.
    assert 0.0 < by_name["frame_core"]["scaled_total_ms"] <= 700.0


def test_bridge_spikes_become_timeline_events(tmp_path: Path) -> None:
    from apm_suite.analysis.events import build_timeline

    session = tmp_path / "session_spikes"
    (session / "app").mkdir(parents=True)
    atomic_json(
        session / "app/apm_app.json",
        {
            "spikes": [
                {
                    "utc": "2026-07-16T10:00:00.000Z",
                    "gmUpdateDurationMs": 250.0,
                    "serverTickIntervalMs": 260.0,
                    "world": {"entities": 500},
                }
            ]
        },
    )
    doc = build_timeline(session)
    spikes = [e for e in doc.events if e.kind == "frame_spike"]
    assert len(spikes) == 1
    assert spikes[0].severity == "error"
    assert "entities=500" in spikes[0].message


def test_bridge_spike_naive_stamp_reads_as_utc_not_local(tmp_path: Path) -> None:
    """A spike stamp without an offset is UTC by repo convention (matching
    session._date and capture._ingest_bridge_snapshot); resolving it in the
    analysis host's local zone would shift frame_spike epochs by the UTC
    offset and drop them from windowed views on non-UTC hosts."""
    from apm_suite.analysis.events import build_timeline

    session = tmp_path / "session_naive_stamp"
    (session / "app").mkdir(parents=True)
    atomic_json(
        session / "app/apm_app.json",
        {"spikes": [{"utc": "2026-07-16T10:00:00", "gmUpdateDurationMs": 250.0}]},
    )
    expected = datetime(2026, 7, 16, 10, 0, tzinfo=UTC).timestamp()
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Tokyo"  # any non-UTC zone proves locality is ignored
        time.tzset()
        doc = build_timeline(session)
        spike = next(e for e in doc.events if e.kind == "frame_spike")
        assert spike.model_dump(mode="json")["t"] == pytest.approx(expected)
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def test_app_scrape_events_withhold_raw_console_text(tmp_path: Path) -> None:
    from apm_suite.analysis.events import build_events

    session = tmp_path / "session_scrape"
    (session / "app").mkdir(parents=True)
    # The telnet drain interleaves bridge output with server log lines that
    # carry player names, IPs, and Steam IDs; events must not echo them.
    pii = "Player 'Alice' joined [203.0.113.7:26900] steamid=76561198000000001"
    records = [
        {
            "t": 1.0,
            "ok": True,
            "text": f"[7dtd-server-apm] SPIKE gmUpdateDuration=250.00ms\n{pii}\n",
        },
        {"t": 2.0, "ok": True, "text": f"spike counter bumped\n{pii}\n"},
    ]
    (session / "app/bridge.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    doc = build_events(session)
    spikes = [e for e in doc.events if e.kind == "managed_bridge_spike"]
    assert len(spikes) == 2
    first, second = spikes
    assert first.message == "managed bridge spike gmUpdateDuration=250.0ms"
    assert first.model_dump(mode="json")["value"] == pytest.approx(250.0)
    assert "raw console text withheld" in second.message
    for event in spikes:
        blob = json.dumps(event.model_dump(mode="json"))
        assert "Alice" not in blob and "203.0.113.7" not in blob
        assert "76561198000000001" not in blob and "joined" not in blob
    # The persisted events files are the export surface; pin them clean too.
    persisted = (session / "events.jsonl").read_text()
    assert "Alice" not in persisted and "203.0.113.7" not in persisted


def test_attribute_document_matches_attribute_snapshot(tmp_path: Path) -> None:
    from apm_suite.analysis.bridge import attribute_document, attribute_snapshot

    session = tmp_path / "session_attr"
    session.mkdir()
    doc = {
        "sections": [
            {"name": "DecoManager.UpdateTick", "totalMs": 400.0, "calls": 2000},
            {"name": "World.TickEntity", "totalMs": 100.0, "calls": 40, "deep": True},
        ],
        "measurement": {"deepSampleRate": 16},
        "update": {"windowUpdates": 100},
        "world": {"entities": 50},
    }
    atomic_json(session / "app/apm_app.json", doc)
    # The doc-level helper (used by build_summary to avoid a re-read) must apply
    # the identical deep-sample scaling as the session-level entry point.
    assert attribute_document(doc) == attribute_snapshot(session)


def test_build_summary_lag_attribution_uses_snapshot(tmp_path: Path) -> None:
    from apm_suite.analysis.report import build_summary

    session = tmp_path / "session_lagattr"
    (session / "app").mkdir(parents=True)
    atomic_json(
        session / "app/apm_app.json",
        {
            "sections": [{"name": "DecoManager.UpdateTick", "totalMs": 900.0, "calls": 2000}],
            "measurement": {"deepSampleRate": 1},
            "update": {"windowUpdates": 100},
            "world": {"entities": 10},
        },
    )
    atomic_json(session / "meta.json", _meta())
    summary = build_summary(session)
    causes = (summary.metadata.get("lag_diagnosis") or {}).get("causes") or []
    # The dominant managed subsystem is named even with no host probe fired.
    assert any(c["cause"] == "deco_world_bound" for c in causes)


def test_build_summary_survives_snapshot_with_non_numeric_fields(tmp_path: Path) -> None:
    """A hand-edited/imported snapshot whose numeric fields hold strings raises
    TypeError from the frame math; that must drop only the snapshot-derived
    blocks, not fail the required summary stage and lose the host evidence."""
    from apm_suite.analysis.report import build_summary

    session = tmp_path / "session_strsnap"
    (session / "app").mkdir(parents=True)
    atomic_json(
        session / "app/apm_app.json",
        {
            "sections": [],
            "measurement": {"deepSampleRate": 1},
            "update": {"windowUpdates": 100, "gmUpdateDurationAvgMs": "3.2"},
            "world": {"entities": 10, "unityDeltaMs": "16.6"},
        },
    )
    atomic_json(session / "meta.json", _meta())
    summary = build_summary(session)  # must not raise
    # Host-side evidence survived; only snapshot-derived blocks were dropped.
    assert [layer.layer for layer in summary.layers] != []


def test_build_summary_keeps_measured_zero_gross_alloc(tmp_path: Path) -> None:
    """grossAllocBytesPerSecond == 0 is real evidence (idle window), never the
    bridge's -1 unmeasured sentinel: it must land in metadata as a healthy zero
    instead of degrading to UNKNOWN, while -1 stays absent."""
    from apm_suite.analysis.report import _snapshot_metadata

    measured = _snapshot_metadata(
        {"gc": {"grossAllocBytesPerSecond": 0, "heapDeltaBytes": 0, "windowSeconds": 30}}, ""
    )
    assert measured["gc"]["grossAllocMBPerSecond"] == 0.0
    unmeasured = _snapshot_metadata(
        {"gc": {"grossAllocBytesPerSecond": -1, "heapDeltaBytes": 0, "windowSeconds": 30}}, ""
    )
    assert "grossAllocMBPerSecond" not in unmeasured["gc"]


def test_parse_managed_sections_reads_each_named_file_once(tmp_path: Path) -> None:
    from apm_suite.analysis.bridge import parse_managed_sections

    session = tmp_path / "session_sections"
    (session / "app").mkdir(parents=True)
    snapshot = {
        "sections": [
            {"name": "GmUpdate", "avgMs": 5.0, "calls": 100},
            {"name": "DecoManager.UpdateTick", "avgMs": 2.0, "calls": 800},
        ]
    }
    extra = session / "app/apm_app.json"
    atomic_json(extra, snapshot)
    # managed_sections.json repeats one section identically and adds another;
    # it is matched BOTH by the explicit name list and the *.json sweep.
    atomic_json(
        session / "app/managed_sections.json",
        {"sections": [{"name": "GmUpdate", "avgMs": 5.0, "calls": 100}]},
    )
    atomic_json(
        session / "app/other_dump.json",
        {"sections": [{"name": "World.SaveWorldState", "avgMs": 9.0, "calls": 3}]},
    )
    sections = parse_managed_sections(session, extra)
    names = [s["name"] for s in sections]
    # Every source contributes; identical dicts still dedupe; nothing is lost
    # or duplicated by the named-list/glob overlap.
    assert sorted(names) == ["DecoManager.UpdateTick", "GmUpdate", "World.SaveWorldState"]


def test_alloc_site_rankings_equal_with_preloaded_text(tmp_path: Path) -> None:
    from apm_suite.analysis.report import _alloc_source_text, top_churn_sites

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "mono_alloc.bt.annotated.txt").write_text(
        "=== top sampled (1/4096, all sizes) (top 20) ===\n"
        "@alloc_bytes[\n"
        "        GC_malloc+0\n"
        "        EntityAlive.updateTasks+0x1\n"
        "]: 12345678\n"
    )
    text = _alloc_source_text(tmp_path)
    assert top_churn_sites(tmp_path, text=text) == top_churn_sites(tmp_path)


def test_jitsym_annotates_hex_against_map(tmp_path: Path) -> None:
    from apm_suite.analysis.jitsym import annotate, load_map

    map_file = tmp_path / "perf-1.map"
    map_file.write_text("41e0155e 100 EntityAlive.updateTasks\n41c30f08 80 AstarManager.FindPath\n")
    starts, entries = load_map(map_file)
    out = annotate("site 0x41e0155e and 0x41c30f20 and 0xdeadbeef", starts, entries)
    assert "EntityAlive.updateTasks+0x0" in out
    assert "AstarManager.FindPath+0x18" in out
    assert "0xdeadbeef" in out  # outside any range: left as-is


def test_diagnose_lag_ranks_causes() -> None:
    from apm_suite.analysis.report import diagnose_lag

    layers = [
        LayerScore(
            layer="sync_locks", score=15, state="collected", signals={"slow_futex_lines": 8}
        ),
        LayerScore(layer="scheduler", score=25, state="collected", signals={"disk_block_ms": 0.0}),
    ]
    metadata = {
        "frame": {"lateTicks": 40, "tickStallMsTotal": 2000},
        "gc": {"grossAllocMBPerSecond": 4.0, "fullCollections": 3, "windowSeconds": 30.0},
    }
    threads = {
        "main_thread_share_of_process_avg": 0.6,
        "main_thread_cpu_pct_avg": 55.0,
        "n_threads": 200,
    }
    result = diagnose_lag(layers, metadata, threads)
    assert result["laggy"] is True
    kinds = [c["cause"] for c in result["causes"]]
    assert kinds[0] == "gc_pauses"  # highest severity first
    assert "main_thread_bound" in kinds
    assert "lock_contention" in kinds
    assert "gc_pauses" in result["verdict"]


def test_diagnose_lag_names_dominant_subsystem() -> None:
    from apm_suite.analysis.report import diagnose_lag

    metadata = {"frame": {"lateTicks": 30, "tickStallMsTotal": 1500}}
    attribution = {"subsystems": [{"subsystem": "network", "share": 0.6}]}
    result = diagnose_lag([], metadata, {}, attribution)
    kinds = [c["cause"] for c in result["causes"]]
    assert "network_bound" in kinds
    net = next(c for c in result["causes"] if c["cause"] == "network_bound")
    assert "B3" in net["fix"]


def test_diagnose_lag_skips_inclusive_frame_core_subsystem() -> None:
    from apm_suite.analysis.report import diagnose_lag

    # frame_core (GameManager.UpdateTick) is inclusive of the others; when it is
    # nominally top, the diagnosis must still name the top DISJOINT subsystem
    # (the player-scale network wall), not fall silent.
    metadata = {"frame": {"lateTicks": 20, "tickStallMsTotal": 2000}}
    attribution = {
        "subsystems": [
            {"subsystem": "frame_core", "share": 0.62},
            {"subsystem": "network", "share": 0.55},
            {"subsystem": "io_saves", "share": 0.09},
        ]
    }
    result = diagnose_lag([], metadata, {}, attribution)
    assert "network_bound" in [c["cause"] for c in result["causes"]]


def test_scaling_detects_superlinear_section(tmp_path: Path) -> None:
    from apm_suite.analysis.scaling import analyze_scaling

    # Three load levels; one section is O(N^2) per call, one is flat.
    sessions = []
    for n in (100, 200, 400):
        s = tmp_path / f"session_n{n}"
        (s).mkdir()
        atomic_json(
            s / "summary.json",
            {
                "schema": "7dtd.apm.summary.v2",
                "session_id": s.name,
                "metadata": {"world": {"clients": n}},
            },
        )
        atomic_json(
            s / "csharp_bridge.json",
            {
                "schema": "7dtd.apm.bridge.v2",
                "top_managed_sections": [
                    {
                        "name": "NetEntityDistribution.OnUpdateEntities",
                        "avgMs": (n / 100) ** 2,
                        "totalMs": (n / 100) ** 2 * n,
                    },
                    {"name": "World.OnUpdateTick", "avgMs": 2.0, "totalMs": 2.0 * n},
                ],
            },
        )
        sessions.append(s)
    result = analyze_scaling(sessions, "players")
    names = {f["section"] for f in result["super_linear"]}
    assert "NetEntityDistribution.OnUpdateEntities" in names
    assert "World.OnUpdateTick" not in names
    quad = next(f for f in result["sections"] if f["section"].startswith("NetEntityDistribution"))
    assert quad["per_call_class"] in ("super-linear", "quadratic+")
    assert quad["per_call_exponent"] == pytest.approx(2.0, abs=0.1)


def test_diagnose_lag_flags_saturation() -> None:
    from apm_suite.analysis.report import diagnose_lag

    # 3400 ms/tick with every tick late = past capacity (the ~0.3 TPS collapse).
    metadata = {
        "frame": {
            "lateTicks": 15,
            "windowUpdates": 15,
            "tickIntervalAvgMs": 3400.0,
            "gmUpdateAvgMs": 1500.0,
        }
    }
    result = diagnose_lag([], metadata, {})
    assert result.get("saturated") is True
    assert "SATURATED" in result["verdict"]
    assert any(c["cause"] == "server_saturated" for c in result["causes"])


def test_diagnose_lag_healthy_when_no_late_ticks() -> None:
    from apm_suite.analysis.report import diagnose_lag

    result = diagnose_lag([], {"frame": {"lateTicks": 0}}, {})
    assert result["laggy"] is False
    assert "met its tick deadline" in result["verdict"]


def _gc_layer_with(signals: dict[str, object]) -> LayerScore:
    return LayerScore(layer="runtime_gc", score=50, state="collected", signals=signals)


def test_diagnose_lag_stw_freeze_vs_incremental() -> None:
    from apm_suite.analysis.report import diagnose_lag

    frame = {"frame": {"lateTicks": 10, "gmUpdateAvgMs": 12.0}}
    # Big freeze: worst STW >= 50ms -> "worst freeze" wording.
    freeze = diagnose_lag(
        [_gc_layer_with({"stw_pause_worst_ms": 320.0, "stw_pause_total_ms": 340.0})],
        {**frame, "gc": {"grossAllocMBPerSecond": 9.0, "fullCollections": 2, "windowSeconds": 60}},
        {},
    )
    gc_cause = next(c for c in freeze["causes"] if c["cause"] == "gc_pauses")
    assert "worst freeze 320.0 ms" in gc_cause["detail"]

    # Low STW but high incremental collect rate -> "incremental GC" wording.
    incremental = diagnose_lag(
        [_gc_layer_with({"stw_pause_worst_ms": 5.0, "collect_a_little_hits": 18000})],
        {**frame, "gc": {"grossAllocMBPerSecond": 9.0, "fullCollections": 1, "windowSeconds": 60}},
        {},
    )
    inc_cause = next(c for c in incremental["causes"] if c["cause"] == "gc_pauses")
    assert "incremental GC" in inc_cause["detail"]


def test_diagnose_lag_profile_spike_vs_compute() -> None:
    from apm_suite.analysis.report import diagnose_lag

    gc = {"gc": {"grossAllocMBPerSecond": 6.0, "fullCollections": 1, "windowSeconds": 60}}
    spike = diagnose_lag([], {"frame": {"lateTicks": 10, "gmUpdateAvgMs": 11.0}, **gc}, {})
    assert "spike-driven" in spike["profile"]
    assert spike["tick_headroom_pct"] > 50

    compute = diagnose_lag([], {"frame": {"lateTicks": 10, "gmUpdateAvgMs": 45.0}, **gc}, {})
    assert "compute-bound" in compute["profile"]


def test_gc_layer_takes_last_cumulative_little_n_and_stw() -> None:
    from apm_suite.analysis.report import _gc_layer

    # @little_n is printed each interval as a growing cumulative; the parser
    # must take the LAST, not the first. STW total/worst come from END markers.
    text = (
        "@little_n: 97\n@little_n: 586\n@little_n: 18068\nSTW_PAUSE 320000 us\n@stw_sum: 340000\n"
    )
    layer = _gc_layer({"mono_gc": text})
    assert layer.signals["collect_a_little_hits"] == 18068
    assert layer.signals["stw_pause_worst_ms"] == 320.0
    assert layer.signals["stw_pause_total_ms"] == 340.0


@pytest.mark.parametrize(
    "text",
    [
        "",  # probe produced nothing (attach failed / server down)
        "garbage line\nno markers here\n",
        "@little_n:\nSTW_PAUSE us\n@stw_sum: notanumber\n",  # truncated / malformed
        "STW_PAUSE 999",  # missing unit suffix
        "@little_n: 5" * 5000,  # pathological repetition, no newlines
    ],
)
def test_gc_layer_survives_malformed_probe_output(text: str) -> None:
    from apm_suite.analysis.report import _gc_layer

    layer = _gc_layer({"mono_gc": text})  # must not raise
    assert layer.layer == "runtime_gc"
    assert isinstance(layer.signals["stw_pause_total_ms"], float)


@pytest.mark.parametrize(
    "text", ["", "@udp_send_bytes:\n", "@udp_send_bytes: notnum\n", "random\ntext\n" * 100]
)
def test_build_summary_net_parse_survives_malformed_io_net(tmp_path: Path, text: str) -> None:
    from apm_suite.analysis.report import build_summary

    session = tmp_path / "session_net"
    (session / "io").mkdir(parents=True)
    (session / "io/io_net.bt.out").write_text(text)
    atomic_json(session / "meta.json", _meta())
    summary = build_summary(session)  # must not raise
    assert summary.session_id == session.name


def test_budget_gross_alloc_gate_and_unknown_not_healthy_zero(tmp_path: Path) -> None:
    from apm_suite.analysis.budget import check

    session = tmp_path / "session_budget"
    session.mkdir()
    # runtime_gc collected but over the gross-alloc limit; gc metadata present.
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": session.name,
            "layers": [{"layer": "runtime_gc", "score": 10, "state": "collected"}],
            "metadata": {"gc": {"grossAllocMBPerSecond": 40.0}},
        },
    )
    budget = {
        "max_layer_scores": {"runtime_gc": 60, "cpu": 50},  # cpu has no evidence
        "max_gross_alloc_mb_per_second": 15.0,
    }
    ok, lines = check(session, budget, None, 15.0)
    assert ok is False
    # gross over budget -> FAIL, and the missing cpu layer is UNKNOWN, not a pass.
    assert any("FAIL max_gross_alloc_mb_per_second=40.0" in ln for ln in lines)
    assert any("UNKNOWN layer cpu" in ln for ln in lines)


def test_budget_absent_gross_is_skipped_not_passed(tmp_path: Path) -> None:
    from apm_suite.analysis.budget import check

    session = tmp_path / "session_budget2"
    session.mkdir()
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": session.name,
            "layers": [{"layer": "runtime_gc", "score": 10, "state": "collected"}],
            "metadata": {"gc": {}},  # gross unmeasured
        },
    )
    budget = {"max_gross_alloc_mb_per_second": 15.0}
    ok, lines = check(session, budget, None, 15.0)
    # Absent gross must be reported as skipped, never silently passed as 0.
    assert any("skip max_gross_alloc_mb_per_second (no data)" in ln for ln in lines)


def _budget_session(root: Path, name: str, layers: tuple[tuple[str, float], ...]) -> Path:
    session = root / name
    session.mkdir()
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": name,
            "layers": [
                {"layer": layer, "score": score, "state": "collected"} for layer, score in layers
            ],
        },
    )
    return session


def test_budget_regression_gate_fails_and_flags_coverage_mismatch(tmp_path: Path) -> None:
    from apm_suite.analysis.budget import check

    base = _budget_session(tmp_path, "budget_base", (("cpu", 50.0), ("runtime_gc", 20.0)))
    candidate = _budget_session(tmp_path, "budget_cand", (("cpu", 70.0), ("runtime_gc", 20.0)))
    ok, lines = check(candidate, {}, base, 15.0)
    assert ok is False
    # +20 on cpu busts the 15% allowance; the unchanged layer stays an ok line.
    assert any("FAIL regression cpu: baseline=50.0 now=70.0" in ln for ln in lines)
    assert any("ok   regression runtime_gc" in ln for ln in lines)

    wider = _budget_session(tmp_path, "budget_cand_wide", (("cpu", 50.0), ("io", 5.0)))
    ok_mismatch, lines_mismatch = check(wider, {}, base, 15.0)
    # Different coverage between baseline and candidate is UNKNOWN, not a pass.
    assert ok_mismatch is False
    assert any("UNKNOWN regression: baseline and candidate" in ln for ln in lines_mismatch)


def test_budget_late_tick_share_and_section_heat_gates(tmp_path: Path) -> None:
    from apm_suite.analysis.budget import check

    late = tmp_path / "session_late"
    late.mkdir()
    atomic_json(
        late / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": late.name,
            "layers": [],
            "metadata": {"frame": {"lateTicks": 2, "windowUpdates": 10}},
        },
    )
    atomic_json(
        late / "csharp_bridge.json",
        {
            "schema": "7dtd.apm.bridge.v2",
            "top_managed_sections": [{"name": "World.TickEntities", "avgMs": 25.0}],
        },
    )
    budget = {"max_late_tick_share": 0.05, "max_section_heat": {"World.TickEntities": 20}}
    ok, lines = check(late, budget, None, 15.0)
    assert ok is False
    assert any("FAIL late_ticks 2/10 = 0.200 > budget 0.05" in ln for ln in lines)
    assert any("FAIL section World.TickEntities=25.0 > budget 20" in ln for ln in lines)

    # No bridge frame data at all: skipped with a reason, not scored as 0.
    bare = tmp_path / "session_late_bare"
    bare.mkdir()
    atomic_json(
        bare / "summary.json",
        {"schema": "7dtd.apm.summary.v2", "session_id": bare.name, "layers": []},
    )
    ok_bare, lines_bare = check(
        bare,
        {"max_late_tick_share": 0.05, "max_section_heat": {"World.TickEntities": 20}},
        None,
        15.0,
    )
    assert ok_bare is True  # nothing failed; the gap is reported instead
    assert any("skip late_ticks (no bridge frame data)" in ln for ln in lines_bare)
    assert any("skip section World.TickEntities (no heat data)" in ln for ln in lines_bare)


def test_layer_requested_shared_alias_table() -> None:
    # One table feeds capture planning, summary scoring, and the audit; these
    # cases pin tokens that previously worked in only some of the three.
    from apm_suite.models import layer_requested

    assert layer_requested("io", {"all"})
    assert layer_requested("io", {"net"})  # was missing from the report-side map
    assert layer_requested("memory_cache", {"proc"})  # was missing from the report-side map
    assert layer_requested("sync_locks", {"futex"})
    assert layer_requested("scheduler", {"sched"})
    assert not layer_requested("io", {"cpu"})


def test_only_tokens_resolve_identically_for_planning_and_audit() -> None:
    """The capture plan and the audit must agree on every (token, collector)
    pair: a disagreement surfaces as false "requested collector produced no
    usable evidence" warnings (or silently missing ones) in every manifest."""
    from apm_suite.capture import SPECS, wanted
    from apm_suite.models import LAYER_ALIASES
    from apm_suite.session import _requested

    tokens = [
        "all",
        *(spec.name for spec in SPECS),
        *(alias for aliases in LAYER_ALIASES.values() for alias in aliases),
        "alloc",
        "allocsites",
        "nonsense",
    ]
    for token in tokens:
        for spec in SPECS:
            assert wanted(spec, token) == _requested(spec.name, spec.layer, {token}), (
                f"plan/audit drift for --only {token!r} on collector {spec.name!r}"
            )


def test_optin_collector_is_not_flagged_by_audit_under_all(tmp_path: Path) -> None:
    """mono_alloc is deliberately excluded from default plans; the audit must
    not warn about it as if it were requested evidence that went missing."""
    from apm_suite.capture import SPECS, wanted

    session = _session(tmp_path / "session_optin_audit")
    alloc = next(spec for spec in SPECS if spec.name == "mono_alloc")
    assert not wanted(alloc, "all")  # never planned under a default capture
    atomic_json(
        session / "runtime" / "mono_alloc.result.json",
        {
            "schema": "7dtd.apm.collector-result.v1",
            "name": "mono_alloc",
            "layer": "runtime_gc",
            "status": "skipped",
            "message": "collector not requested",
        },
    )
    manifest, valid = audit_session(session)
    assert not any("mono_alloc" in warning for warning in manifest.warnings)
    assert valid


def test_layer_alias_token_selects_whole_layer_in_the_plan() -> None:
    """--only net means the io layer everywhere: plan, audit, and summary all
    treat it as requesting vfs/block/io_net, not io_net alone."""
    from apm_suite.capture import SPECS, wanted

    planned = {spec.name for spec in SPECS if wanted(spec, "net")}
    assert planned == {"vfs", "block", "io_net"}


def test_threads_token_keeps_proc_ridealong_in_plan_and_audit() -> None:
    """--only threads also samples /proc thread stats by design; both sides
    must count proc as requested so a failed scrape is audited as a gap."""
    from apm_suite.capture import SPECS, wanted
    from apm_suite.session import _requested

    proc = next(spec for spec in SPECS if spec.name == "proc")
    assert wanted(proc, "threads")
    assert _requested(proc.name, proc.layer, {"threads"})


def test_build_summary_marks_only_requested_layers_collected(tmp_path: Path) -> None:
    from apm_suite.analysis.report import build_summary

    session = tmp_path / "session_alias"
    (session / "sync").mkdir(parents=True)
    (session / "sync/futex.bt.out").write_text("SLOW_FUTEX tid=1 wait=9ms\n")
    atomic_json(session / "meta.json", _meta(only="locks"))
    summary = build_summary(session)
    by_layer = {layer.layer: layer for layer in summary.layers}
    assert by_layer["sync_locks"].state == "collected"
    assert by_layer["sync_locks"].score is not None
    assert by_layer["cpu"].state == "skipped"  # not requested -> no fake zero
    assert by_layer["cpu"].score is None


# --- session index page --------------------------------------------------------


def test_index_html_navigation_and_empty_state(tmp_path: Path) -> None:
    from apm_suite.analysis.index import html_index

    # Empty store: the page must tell the user how to produce a session
    # instead of showing an empty table with no guidance.
    empty = html_index([])
    assert "No sessions yet" in empty
    assert "7dtd-server-apm capture" in empty

    # A session without rendered pages must not link to a nonexistent file.
    bare_dir = tmp_path / "session_bare"
    bare_dir.mkdir()
    (bare_dir / "summary.json").write_text("{}")
    rows = [
        {"dir": "session_bare", "path": str(bare_dir), "has_dashboard": False, "has_report": False}
    ]
    html_out = html_index(rows)
    assert ">session_bare</a>" not in html_out  # no link to a nonexistent page

    # Artifact glyphs are links to the artifacts they advertise.
    rows = [
        {
            "dir": "session_full",
            "path": str(tmp_path / "session_full"),
            "has_dashboard": True,
            "has_report": False,
            "has_flame": True,
            "has_bridge": True,
        }
    ]
    html_out = html_index(rows)
    assert 'href="session_full/dashboard.html">session_full</a>' in html_out
    assert 'href="session_full/cpu/perf/flame.html"' in html_out
    assert 'href="session_full/csharp_bridge.md"' in html_out


# --- golden report -------------------------------------------------------------


def test_golden_report_render(tmp_path: Path) -> None:
    session = tmp_path / "session_golden"
    session.mkdir()
    atomic_json(
        session / "summary.json",
        {
            **_summary(
                "session_golden",
                [
                    {
                        "layer": "cpu",
                        "score": 42,
                        "state": "collected",
                        "signals": {"ipc": 0.8},
                        "optimize": ["Reduce work on main sim thread"],
                    }
                ],
            ),
            "recommendation": "Focus on cpu",
            "meta": _meta(),
        },
    )
    atomic_json(
        session / "events.json",
        _events(
            "session_golden",
            [{"kind": "cpu_spike", "severity": "warn", "message": "process cpu%=200"}],
        ),
    )
    render_session(session)
    golden = (FIXTURES / "golden_report.html").read_text()
    assert (session / "report.html").read_text() == golden


def test_templates_escape_runtime_content(tmp_path: Path) -> None:
    session = tmp_path / "session_escape"
    session.mkdir()
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": session.name,
            "recommendation": "<script>alert(1)</script>",
            "layers": [{"layer": "cpu", "score": 10, "signals": {}, "optimize": []}],
        },
    )
    atomic_json(
        session / "events.json",
        {"events": [{"kind": "x", "severity": "warn", "message": "<img src=x>"}]},
    )
    render_session(session)
    html = (session / "dashboard.html").read_text()
    report = (session / "report.html").read_text()
    assert "<img src=x>" not in html
    assert "<script>alert(1)</script>" not in report
    assert "&lt;script&gt;" in report


# --- integration: finalize pipeline on a synthetic session ---------------------


def test_finalize_pipeline_end_to_end(tmp_path: Path) -> None:
    session = tmp_path / "session_integration"
    (session / "sync").mkdir(parents=True)
    (session / "memory").mkdir()
    atomic_json(session / "meta.json", _meta(seconds=10))
    (session / "sync/futex.bt.out").write_text("SLOW_FUTEX tid=1 wait=9ms\n@wait_n: 3\n")
    (session / "memory/proc.jsonl").write_text(
        json.dumps({"t": 1.0, "cpu_pct": 200.0, "rss_mb": 100.0}) + "\n"
    )
    result = finalize(session)
    assert result.exit_code == 0, result.failed_stages
    for artifact in REQUIRED:
        assert (session / artifact).is_file(), f"missing {artifact}"
    manifest, valid = audit_session(session)
    assert valid, manifest.errors
    summary = load_json(session / "summary.json")
    assert summary.get("health") is None  # health.json is the single home of health
    sync = next(layer for layer in summary["layers"] if layer["layer"] == "sync_locks")
    assert sync["state"] == "collected"
    assert sync["signals"]["slow_futex_lines"] == 1
    health = load_json(session / "health.json")
    assert health["confidence"] == "insufficient"  # partial coverage never grades


def test_compare_rejects_different_layer_coverage(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    atomic_json(
        before / "summary.json",
        {
            "meta": {"only": "cpu", "seconds": 10},
            "layers": [{"layer": "cpu", "score": 10, "state": "collected"}],
        },
    )
    atomic_json(
        after / "summary.json",
        {
            "meta": {"only": "cpu,io", "seconds": 10},
            "layers": [
                {"layer": "cpu", "score": 9, "state": "collected"},
                {"layer": "io", "score": 1, "state": "collected"},
            ],
        },
    )
    result = runner.invoke(app, ["compare", str(before), str(after)])
    assert result.exit_code == 1  # ValueError from compare_sessions -> exit 1
    assert "incompatible layer coverage" in result.stderr


def _cmp_session(
    root: Path,
    name: str,
    *,
    seconds: float = 30.0,
    version: str = "2.1.0",
    layers: tuple[tuple[str, float], ...] = (("cpu", 40.0),),
    workload: dict[str, object] | None = None,
) -> Path:
    session = root / name
    session.mkdir()
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": name,
            "layers": [
                {"layer": layer, "score": score, "state": "collected"} for layer, score in layers
            ],
            "meta": {"analyzer_version": version, "only": "all", "seconds": seconds},
        },
    )
    if workload is not None:
        atomic_json(session / "workload.json", workload)
    return session


@pytest.mark.parametrize(
    "kwargs_a,kwargs_b,message",
    [
        ({"version": "2.1.0"}, {"version": "2.2.0"}, "analyzer versions differ"),
        ({"seconds": 4}, {"seconds": 4}, "capture too short"),
        ({"seconds": 10}, {"seconds": 30}, "differ by more than 10%"),
        (
            {"workload": {"mode": "clients"}},
            {},
            "only one session has a workload manifest",
        ),
        (
            {"workload": {"mode": "clients", "target": "standard"}},
            {"workload": {"mode": "clients", "target": "deep"}},
            "workload manifests are not equivalent",
        ),
    ],
)
def test_compare_rejects_mismatched_session_pairs(
    tmp_path: Path, kwargs_a: dict[str, object], kwargs_b: dict[str, object], message: str
) -> None:
    from apm_suite.analysis.compare import compare_sessions

    a = _cmp_session(tmp_path, "cmp_guard_a", **kwargs_a)  # type: ignore[arg-type]
    b = _cmp_session(tmp_path, "cmp_guard_b", **kwargs_b)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=message):
        compare_sessions(a, b)


def test_compare_marks_one_sided_section_not_comparable(tmp_path: Path) -> None:
    from apm_suite.analysis.compare import compare_sessions

    a = _cmp_session(tmp_path, "cmp_sec_a")
    b = _cmp_session(tmp_path, "cmp_sec_b")
    # The section exists only in B: the other side never exercised it, so the
    # delta must be flagged, not ranked as an improvement over an implied 0.
    atomic_json(
        b / "csharp_bridge.json",
        {
            "schema": "7dtd.apm.bridge.v2",
            "top_managed_sections": [{"name": "Solo.Section", "avgMs": 5.0}],
        },
    )
    result = compare_sessions(a, b)
    row = next(d for d in result["section_deltas"] if d["section"] == "Solo.Section")
    assert row == {
        "section": "Solo.Section",
        "a_heat": 0.0,
        "b_heat": 5.0,
        "delta_b_minus_a": 5.0,
        "better": "not_comparable",
    }


def test_partial_capture_withholds_health_grade(tmp_path: Path) -> None:
    session = tmp_path / "session_partial"
    session.mkdir()
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": session.name,
            "layers": [
                {"layer": "cpu", "score": 10, "state": "collected"},
                {"layer": "io", "score": None, "state": "unavailable"},
            ],
        },
    )
    build_health(session)
    health = load_json(session / "health.json")
    assert health["health"] is None
    assert health["grade"] is None
    assert health["confidence"] == "insufficient"


ALL_KNOWN_LAYERS = (
    "sync_locks",
    "runtime_gc",
    "cpu",
    "app_sim",
    "io",
    "memory_cache",
    "scheduler",
)


@pytest.mark.parametrize(
    "score,expected_grade",
    [
        (10, "A"),
        (15, "A"),  # health 85: grade A boundary is inclusive
        (16, "B"),
        (30, "B"),  # health 70: grade B boundary is inclusive
        (31, "C"),
        (45, "C"),  # health 55: grade C boundary is inclusive
        (46, "D"),
        (60, "D"),  # health 40: grade D boundary is inclusive
        (61, "F"),
    ],
)
def test_compute_health_grades_full_coverage_by_pressure(score: float, expected_grade: str) -> None:
    from apm_suite.analysis.health import compute_health

    result = compute_health(dict.fromkeys(ALL_KNOWN_LAYERS, score))
    # Uniform scores across all seven known layers -> health = 100 - score.
    assert result.health == 100 - score
    assert result.grade == expected_grade
    assert result.confidence == "medium"
    assert result.coverage == pytest.approx(1.0)


def test_compute_health_withholds_grade_below_and_at_eighty_percent_coverage() -> None:
    from apm_suite.analysis.health import DEFAULT_WEIGHT, WEIGHTS, compute_health

    # sync_locks+runtime_gc+cpu+io+memory_cache = 0.72 weighted coverage.
    partial = {name: 20.0 for name in ("sync_locks", "runtime_gc", "cpu", "io", "memory_cache")}
    below = compute_health(partial)
    assert below.health is None and below.grade is None
    assert below.confidence == "insufficient"

    # One unknown layer adds DEFAULT_WEIGHT; the same set lands on exactly 0.80,
    # which must be graded, not withheld (< COVERAGE_MIN is strict).
    at_threshold = {**partial, "custom_probe": 20.0}
    weight_sum = sum(WEIGHTS.get(n, DEFAULT_WEIGHT) for n in at_threshold)
    assert weight_sum == pytest.approx(0.80)
    graded = compute_health(at_threshold)
    assert graded.grade is not None
    assert graded.coverage == pytest.approx(0.8)


def test_compute_health_clamps_out_of_range_scores() -> None:
    from apm_suite.analysis.health import compute_health

    # Unvalidated hand-edited summary scores must not push health out of range.
    pegged = dict.fromkeys(ALL_KNOWN_LAYERS, 0.0)
    pegged["cpu"] = 250.0  # clamps to pressure 100 -> 0.15 * 100 weighted
    hot = compute_health(pegged)
    assert hot.pressure == pytest.approx(15.0)
    assert hot.health == pytest.approx(85.0)

    negative = dict.fromkeys(ALL_KNOWN_LAYERS, 50.0)
    negative["cpu"] = -5.0  # clamps to pressure 0; the other six weigh 0.85 * 50
    cold = compute_health(negative)
    assert cold.pressure == pytest.approx(42.5)


# --- bridge source structure -----------------------------------------------------


def test_bridge_exports_off_update_thread_and_uses_v3_schema() -> None:
    source = (REPO / "bridge/ApmBridge/Telemetry.cs").read_text()
    end_frame = source.split("public static void EndFrame()", 1)[1].split(
        "static void AddSpike", 1
    )[0]
    assert "File.Write" not in end_frame
    assert "QueueLatest()" in end_frame
    assert 'schema = "7dtd.apm.app.v3"' in source
    assert "serverTickIntervalAvgMs" in source


# --- malformed analysis records --------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        {"t": 1.0, "cpu_pct": "999", "rss_mb": 100.0},  # string cpu (coerced)
        {"t": 1.0, "cpu_pct": None, "rss_mb": 100.0},  # null cpu
        {"t": 1.0, "rss_mb": 100.0},  # missing cpu
        {"t": 1.0, "cpu_pct": 200.0, "rss_mb": "big"},  # string rss
        {"t": 1.0, "cpu_pct": 200.0, "rss_mb": None},  # null rss
    ],
)
def test_parse_proc_jsonl_survives_non_numeric_fields(
    tmp_path: Path, record: dict[str, object]
) -> None:
    from apm_suite.analysis.events import EventSink, parse_proc_jsonl

    session = tmp_path / "session_proc"
    (session / "memory").mkdir(parents=True)
    (session / "memory/proc.jsonl").write_text(json.dumps(record) + "\n")
    sink = EventSink()
    parse_proc_jsonl(sink, session / "memory/proc.jsonl")  # must not raise
    assert all(isinstance(e["value"], (int, float)) for e in sink.events)


def _write_proc_jsonl(session: Path, records: list[dict[str, object]]) -> None:
    session.mkdir(parents=True)
    (session / "memory").mkdir()
    (session / "memory/proc.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )


def test_memory_trend_ignores_unknown_fd_sentinel(tmp_path: Path) -> None:
    """fd_count=-1 means the sample could not list /proc/pid/fd; treating it as
    a count manufactured fd growth (e.g. 150-(-1)=151) and fired false leak
    causes. Endpoints must come from measured counts only."""
    from apm_suite.analysis.report import memory_trend

    session = tmp_path / "session_fd_race"
    _write_proc_jsonl(
        session,
        [
            {"t": 1.0, "rss_mb": 1000.0, "fd_count": -1},  # raced listing
            {"t": 2.0, "rss_mb": 1000.5, "fd_count": 40},
            {"t": 3.0, "rss_mb": 1001.0, "fd_count": 150},
        ],
    )
    trend = memory_trend(session)
    assert trend["fd_start"] == 40
    assert trend["fd_end"] == 150


def test_memory_trend_omits_fds_when_every_sample_is_unknown(tmp_path: Path) -> None:
    from apm_suite.analysis.report import memory_trend

    session = tmp_path / "session_fd_unknown"
    _write_proc_jsonl(
        session,
        [
            {"t": 1.0, "rss_mb": 1000.0, "fd_count": -1},
            {"t": 2.0, "rss_mb": 1000.5, "fd_count": None},
            {"t": 3.0, "rss_mb": 1001.0, "fd_count": -1},
        ],
    )
    trend = memory_trend(session)
    assert "fd_start" not in trend
    assert "fd_end" not in trend
    assert trend["rss_growth_mb_per_s"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    "wchan_top",
    [
        {"futex_wait": "5"},  # string count (coerced -> emits)
        {"futex_wait": None},  # null count (skipped)
        {"futex_wait": 4},  # numeric passes through
        {},  # empty
    ],
)
def test_parse_threads_jsonl_survives_non_numeric_waiters(
    tmp_path: Path, wchan_top: dict[str, object]
) -> None:
    from apm_suite.analysis.events import EventSink, parse_threads_jsonl

    session = tmp_path / "session_threads"
    (session / "threads").mkdir(parents=True)
    (session / "threads/threads.jsonl").write_text(
        json.dumps({"t": 1.0, "wchan_top": wchan_top}) + "\n"
    )
    sink = EventSink()
    parse_threads_jsonl(sink, session / "threads/threads.jsonl")  # must not raise
    assert all(isinstance(e["value"], int) for e in sink.events)


# --- flame delta -----------------------------------------------------------------


def test_flame_load_weights_and_delta_rank_by_abs(tmp_path: Path) -> None:
    from apm_suite.analysis.flame_delta import delta, load_weights

    a = tmp_path / "a.folded"
    a.write_text(
        "alpha 5\n"
        "alpha 3\n"  # duplicate frame accumulates -> 8
        "\n"  # blank line ignored
        "beta notanumber\n"  # non-numeric count skipped
        "gamma inf\n"  # non-finite count skipped (no int(inf) OverflowError)
        "delta 2\n"
    )
    b = tmp_path / "b.folded"
    b.write_text("alpha 1\ndelta 2\n")
    wa, wb = load_weights(a), load_weights(b)
    assert wa["alpha"] == 8
    assert wa["delta"] == 2
    assert "beta" not in wa and "gamma" not in wa
    rows = delta(wa, wb)
    assert rows[0]["frame"] == "alpha"
    assert abs(rows[0]["delta"]) == 7
    abs_deltas = [abs(int(r["delta"])) for r in rows]
    assert abs_deltas == sorted(abs_deltas, reverse=True)


def test_flame_delta_ties_rank_by_frame_name() -> None:
    """Equal-|delta| frames must keep one stable order: set iteration is hash
    randomized per process, so without the name tiebreak the truncated top-N
    (and thus compare.json) differs run to run."""
    from apm_suite.analysis.flame_delta import delta

    # Every frame moves by exactly -3; only the name tiebreak can order them.
    names = ["zeta", "mu", "alpha", "kappa", "beta", "omega"]
    a = {name: 3 for name in names}
    rows = delta(a, {}, top=30)
    assert [r["frame"] for r in rows] == sorted(names)
    cut = delta(a, {}, top=2)
    assert [r["frame"] for r in cut] == ["alpha", "beta"]


# --- non-finite sample weights must never crash a load ----------------------------


def test_bridge_load_folded_frames_skips_non_finite_counts(tmp_path: Path) -> None:
    from apm_suite.analysis.bridge import load_folded_frames

    folded = tmp_path / "cpu/perf/stacks.folded"
    folded.parent.mkdir(parents=True)
    folded.write_text(
        "alpha;beta 5\n"
        "gamma inf\n"  # int(inf) raises OverflowError, not ValueError
        "delta nan\n"  # NaN is not a sample count either
        "alpha 2\n"
    )
    assert dict(load_folded_frames(tmp_path)) == {"alpha": 7, "beta": 5}


def test_bridge_load_speedscope_frames_skips_non_finite_weights(tmp_path: Path) -> None:
    from apm_suite.analysis.bridge import load_speedscope_frames

    profile = tmp_path / "cpu/perf/profile.speedscope.json"
    profile.parent.mkdir(parents=True)
    # json.loads accepts Infinity/NaN literals; a corrupt weight must not reach
    # int() and crash the whole analysis.
    profile.write_text(
        json.dumps(
            {
                "shared": {"frames": [{"name": "a"}, {"name": "b"}]},
                "profiles": [
                    {
                        "type": "sampled",
                        "samples": [[0], [1], [0]],
                        "weights": [4, float("inf"), float("nan")],
                    }
                ],
            }
        )
    )
    assert dict(load_speedscope_frames(tmp_path)) == {"a": 4}


def test_folded_to_speedscope_load_folded_skips_non_finite_counts(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "folded_to_speedscope", REPO / "tools/host_profiler/folded_to_speedscope.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folded = tmp_path / "stacks.folded"
    folded.write_text("alpha;beta 5\ngamma inf\ndelta nan\nalpha 2\n")
    assert module.load_folded(folded) == [(["alpha", "beta"], 5), (["alpha"], 2)]


def test_annotate_stacks_leaves_non_finite_count_lines_untouched() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "annotate_stacks", REPO / "tools/host_profiler/annotate_stacks.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    line = "GameManager.gmUpdate inf"
    assert module.annotate_folded_line(line) == line


def test_correlate_parse_ts_converts_log_stamps_via_local_zone_rules() -> None:
    """Server log stamps carry no offset field, so parse_ts must resolve them
    with this host's zone rules (DST included); stamping them as UTC shifts
    every spike correlation by the local UTC offset on non-UTC hosts."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "correlate", REPO / "tools/host_profiler/correlate.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # wall stamp -> the UTC hour that wall time denotes in that zone on that
    # date (independent of the code under test): JST=CET=UTC+1h, CEST=UTC+2h.
    cases = [
        ("Asia/Tokyo", "2026-01-15T12:00:00", 3),
        ("Europe/Warsaw", "2026-01-15T12:00:00", 11),  # CET
        ("Europe/Warsaw", "2026-07-01T12:00:00", 10),  # CEST, not a fixed +01:00
    ]
    original_tz = os.environ.get("TZ")
    try:
        for zone, wall, utc_hour in cases:
            os.environ["TZ"] = zone
            time.tzset()
            year, month, rest = wall.split("-")
            day = int(rest[:2])
            expected = datetime(int(year), int(month), day, utc_hour, tzinfo=UTC).timestamp()
            assert module.parse_ts(wall) == pytest.approx(expected)
        assert module.parse_ts("not-a-timestamp") == 0.0
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def test_correlate_nearest_proc_binary_search_matches_linear_scan() -> None:
    """nearest_proc over sorted rows must return the same sample as a full scan
    (including the earlier-sample tie-break) while the spike-window membership
    check stays an exact < 5s test at both boundaries."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "correlate", REPO / "tools/host_profiler/correlate.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rows = [{"t": t, "cpu_pct": 200.0} for t in (10.0, 20.0, 30.0, 40.0)]
    times = [r["t"] for r in rows]
    for query in (-5.0, 9.999, 10.0, 14.9, 15.0, 25.0, 39.999, 40.0, 100.0):
        expected = min(rows, key=lambda r: abs(r["t"] - query))
        assert module.nearest_proc(times, rows, query) == expected, query
    assert module.nearest_proc([], [], 1.0) is None

    # Window membership: exactly 5s away is outside (< 5), just inside counts.
    spike_ts = sorted(s for s in (12.0, 34.0))
    inside = [8.0, 16.99, 29.01, 38.99]
    outside = [6.99, 17.01, 23.0, 39.01]
    for t in inside:
        index = module.bisect_left(spike_ts, t - 5)
        assert index < len(spike_ts) and spike_ts[index] < t + 5, t
    for t in outside:
        index = module.bisect_left(spike_ts, t - 5)
        assert not (index < len(spike_ts) and spike_ts[index] < t + 5), t


def test_app_scrape_session_persists_only_command_responses() -> None:
    """The telnet drain must keep protocol replies but discard the pre-auth
    banner, the post-logon reply, and any player-identifying stream content,
    including stream lines interleaved into a command window or split across
    reads."""
    import importlib.util
    import socket
    import threading

    spec = importlib.util.spec_from_file_location(
        "app_scrape", REPO / "tools/apm/collectors/app_scrape.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    received: list[bytes] = []

    def server(listener: socket.socket) -> None:
        ready.set()
        conn, _ = listener.accept()
        try:
            with conn:
                conn.sendall(b"greeting Player 'Alice' from 203.0.113.7\n")
                received.append(conn.recv(1024))  # password line
                conn.sendall(b"Logon successful.\n")
                received.append(conn.recv(1024))  # apm status
                # Genuine reply, then a streamed log line whose tail arrives
                # only after the next command is sent (mid-line TCP split).
                conn.sendall(b"frameAvg=41.2ms spikes=3\n")
                conn.sendall(
                    b"2026-08-23T10:00:00 42.0 INF Player 'Bob' joined "
                    b"[198.51.100.9] steamid=76561198"
                )
                received.append(conn.recv(1024))  # apm dump
                conn.sendall(b"000002\nGmUpdate=5.0ms(x100,max=9.0)\n")
        except OSError:
            pass

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    ready = threading.Event()
    thread = threading.Thread(target=server, args=(listener,), daemon=True)
    thread.start()
    try:
        text = module.session(
            "127.0.0.1", port, "secret-pass", ["apm status", "apm dump"], timeout=2.0
        )
    finally:
        thread.join(timeout=5)
        listener.close()

    assert received[0].strip() == b"secret-pass"  # logon still happens
    assert b"apm status" in received[1] and b"apm dump" in received[2]
    assert ">>> apm status" in text and "frameAvg=41.2ms" in text
    assert "GmUpdate=5.0ms" in text
    for leaked in (
        "greeting",
        "Alice",
        "203.0.113.7",
        "Logon successful",
        "INF Player",
        "Bob",
        "198.51.100.9",
        "76561198000000002",
    ):
        assert leaked not in text


def test_app_scrape_keeps_utf8_split_across_reads() -> None:
    """A multi-byte UTF-8 sequence split across TCP reads must survive intact.

    Decoding per chunk would turn each half of a split character into U+FFFD;
    the reassembly buffer stays bytes so decode happens per complete line.
    """
    import importlib.util
    import socket
    import threading
    import time as time_module

    spec = importlib.util.spec_from_file_location(
        "app_scrape", REPO / "tools/apm/collectors/app_scrape.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def server(listener: socket.socket) -> None:
        ready.set()
        conn, _ = listener.accept()
        try:
            with conn:
                conn.sendall(b"greeting\n")
                conn.recv(1024)  # password line
                conn.recv(1024)  # apm status
                # "é☃😀" split mid-character: first send ends inside é's
                # lead/continuation boundary.
                conn.sendall(b"GmUpdate=2.0ms player=Jos\xc3")
                time_module.sleep(0.2)
                conn.sendall(b"\xa9\xe2\x98\x83\xf0\x9f\x98\x80 done\n")
        except OSError:
            pass

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    ready = threading.Event()
    thread = threading.Thread(target=server, args=(listener,), daemon=True)
    thread.start()
    try:
        text = module.session("127.0.0.1", port, "pw", ["apm status"], timeout=2.0)
    finally:
        thread.join(timeout=5)
        listener.close()

    assert "José☃😀 done" in text
    assert "\ufffd" not in text


def test_flamegraph_svg_survives_non_finite_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "flamegraph", REPO / "tools/host_profiler/flamegraph.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folded = tmp_path / "stacks.folded"
    folded.write_text("alpha;beta 5\nalpha nan\nalpha inf\n")
    out = tmp_path / "flame.svg"
    monkeypatch.setattr(sys, "argv", ["flamegraph.py", str(folded), str(out)])
    assert module.main() == 0
    svg = out.read_text()
    assert "nan" not in svg and "inf<" not in svg


# --- scaling classification ------------------------------------------------------


@pytest.mark.parametrize(
    "exponent,expected",
    [
        (2.0, "quadratic+"),
        (1.7, "quadratic+"),  # QUADRATIC boundary is inclusive
        (1.69, "super-linear"),
        (1.3, "super-linear"),  # SUPERLINEAR boundary is inclusive
        (1.29, "linear"),
        (0.8, "linear"),  # LINEAR_LOW boundary is inclusive
        (0.79, "sub-linear"),
        (0.0, "sub-linear"),
    ],
)
def test_scaling_classify_boundaries(exponent: float, expected: str) -> None:
    from apm_suite.analysis.scaling import classify

    assert classify(exponent) == expected


def test_scaling_zero_ms_section_does_not_crash(tmp_path: Path) -> None:
    from apm_suite.analysis.scaling import analyze_scaling

    # A section that is 0 ms at every load would hit math.log(0); the y>0 filter
    # in _loglog_slope must keep analyze_scaling from crashing.
    sessions = []
    for n in (100, 200, 400):
        s = tmp_path / f"session_z{n}"
        s.mkdir()
        atomic_json(
            s / "summary.json",
            {
                "schema": "7dtd.apm.summary.v2",
                "session_id": s.name,
                "metadata": {"world": {"clients": n}},
            },
        )
        atomic_json(
            s / "csharp_bridge.json",
            {
                "schema": "7dtd.apm.bridge.v2",
                "top_managed_sections": [{"name": "Idle.Section", "avgMs": 0.0, "totalMs": 0.0}],
            },
        )
        sessions.append(s)
    result = analyze_scaling(sessions, "players")  # must not raise
    assert result["schema"] == "7dtd.apm.scaling.v1"
    # No fittable exponent for an all-zero section -> excluded from findings.
    assert all(f["section"] != "Idle.Section" for f in result["sections"])


# --- compare sessions ------------------------------------------------------------


@pytest.mark.parametrize(
    "delta_value,expected",
    [
        (-0.02, "B"),
        (-0.01, "tie"),  # boundary: not strictly < -0.01
        (0.0, "tie"),
        (0.01, "tie"),  # boundary: not strictly > 0.01
        (0.02, "A"),
    ],
)
def test_compare_winner_boundaries(delta_value: float, expected: str) -> None:
    from apm_suite.analysis.compare import _winner

    assert _winner(delta_value) == expected


def test_compare_sessions_ranks_layer_improvement(tmp_path: Path) -> None:
    from apm_suite.analysis.compare import compare_sessions

    def make(name: str, cpu_score: float) -> Path:
        s = tmp_path / name
        s.mkdir()
        atomic_json(
            s / "summary.json",
            {
                "schema": "7dtd.apm.summary.v2",
                "session_id": name,
                "layers": [
                    {"layer": "cpu", "score": cpu_score, "state": "collected"},
                    {"layer": "runtime_gc", "score": 20.0, "state": "collected"},
                ],
                "meta": {"analyzer_version": "2.1.0", "only": "all", "seconds": 30},
                "metadata": {"frame": {"lateTicks": 0}},
            },
        )
        return s

    a, b = make("cmp_a", 50.0), make("cmp_b", 30.0)  # B lowers cpu pressure
    result = compare_sessions(a, b)
    assert result["schema"] == "7dtd.apm.compare.v2"
    assert result["overall_better"] == "B"
    cpu = next(d for d in result["layer_deltas"] if d["layer"] == "cpu")
    assert cpu["better"] == "B"
    assert cpu["delta_b_minus_a"] == -20.0
    gc = next(d for d in result["layer_deltas"] if d["layer"] == "runtime_gc")
    assert gc["better"] == "tie"  # identical scores


# --- session pruning --------------------------------------------------------------


def _prune_store(root: Path, count: int, payload: int = 1) -> None:
    for i in range(count):
        session = root / f"session_{i}"
        session.mkdir()
        (session / "summary.json").write_text("x" * payload)
        stamp = 1_700_000_000 + i * 100
        os.utime(session, (stamp, stamp))  # deterministic age order


def test_prune_dry_run_lists_and_real_prune_keeps_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "apm"
    root.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(root))
    _prune_store(root, 5)

    dry = runner.invoke(app, ["prune", "--keep", "2", "--dry-run"])
    assert dry.exit_code == 0
    # Dry run deletes nothing and names exactly the three oldest.
    assert sorted(p.name for p in root.glob("session_*")) == [f"session_{i}" for i in range(5)]
    assert dry.stdout.count("would remove") == 3
    assert "session_4" not in dry.stdout and "session_3" not in dry.stdout

    real = runner.invoke(app, ["prune", "--keep", "2"])
    assert real.exit_code == 0
    assert sorted(p.name for p in root.glob("session_*")) == ["session_3", "session_4"]


def test_prune_size_budget_removes_oldest_kept_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "apm"
    root.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(root))
    _prune_store(root, 3, payload=1000)

    # Three 1000-byte sessions total 3000 bytes; a 2500-byte budget must evict
    # the OLDEST kept session first, stopping as soon as the total fits.
    result = runner.invoke(app, ["prune", "--keep", "3", "--max-gb", str(2500 / 1024**3)])
    assert result.exit_code == 0
    assert sorted(p.name for p in root.glob("session_*")) == ["session_1", "session_2"]


def test_prune_continues_past_undeletable_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One undeletable session (e.g. EBUSY from a leaked mono bind mount) must
    not abort the prune run and strand the remaining deletions."""
    root = tmp_path / "apm"
    root.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(root))
    _prune_store(root, 4)
    stuck = root / "session_1"

    real_replace = os.replace

    def failing_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        if Path(src) == stuck:
            raise OSError(16, "Device or resource busy")
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    result = runner.invoke(app, ["prune", "--keep", "2"])

    assert result.exit_code == 0
    # session_1 (stuck) survives at the root; the other doomed session is
    # parked in the recovery trash.
    assert sorted(p.name for p in root.glob("session_*")) == ["session_1", "session_2", "session_3"]
    assert (root / ".trash" / "session_0").is_dir()
    assert "could not remove" in result.stderr
    assert str(stuck) in result.stderr


def test_prune_trash_keeps_sessions_recoverable_until_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning is a mass-destruction path, so removed sessions must stay
    recoverable from the store's trash until the grace window elapses."""
    import time as time_mod

    root = tmp_path / "apm"
    root.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(root))
    _prune_store(root, 5)

    first = runner.invoke(app, ["prune", "--keep", "2"])
    assert first.exit_code == 0
    assert sorted(p.name for p in root.glob("session_*")) == ["session_3", "session_4"]
    trash = root / ".trash"
    assert sorted(p.name for p in trash.glob("session_*")) == [
        "session_0",
        "session_1",
        "session_2",
    ]
    # Recoverable means intact: a plain mv back restores usable evidence.
    assert (trash / "session_0" / "summary.json").read_text() == "x"

    # Entries inside the grace window survive the next prune run; expired ones
    # (and only those) are unlinked even when nothing new is pruned.
    stale = time_mod.time() - 25 * 3600
    os.utime(trash / "session_0", (stale, stale))
    second = runner.invoke(app, ["prune", "--keep", "2"])
    assert second.exit_code == 0
    assert not (trash / "session_0").exists()
    assert (trash / "session_1").is_dir() and (trash / "session_2").is_dir()


def test_prune_grace_zero_restores_hard_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APM_PRUNE_GRACE_HOURS=0 opts out of the recovery window entirely."""
    root = tmp_path / "apm"
    root.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(root))
    monkeypatch.setenv("APM_PRUNE_GRACE_HOURS", "0")
    _prune_store(root, 4)

    result = runner.invoke(app, ["prune", "--keep", "2"])
    assert result.exit_code == 0
    assert sorted(p.name for p in root.glob("session_*")) == ["session_2", "session_3"]
    assert not (root / ".trash").exists()


# --- resource lifecycle ------------------------------------------------------------


def test_terminate_tree_kills_launcher_and_grandchild() -> None:
    """The group kill must reach grandchildren: a launcher shell that dies alone
    orphans its bot binary (which keeps sockets open) until its own timeout."""
    launcher = subprocess.Popen(
        ["bash", "-c", 'sleep 30 & wait "$!"'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    grandchildren: list[psutil.Process] = []
    while time.monotonic() < deadline:
        grandchildren = psutil.Process(launcher.pid).children()
        if grandchildren:
            break
        time.sleep(0.05)
    assert grandchildren, "test setup failed: grandchild never appeared"

    reaped = terminate_tree(launcher, term_grace=1)

    assert reaped is not None
    assert launcher.poll() is not None
    deadline = time.monotonic() + 5
    while any(child.is_running() for child in grandchildren) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(child.is_running() for child in grandchildren)


def test_run_redacts_password_flags_from_echoed_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run() echoes every command it executes into the capture log; the value
    after a password flag must be masked there, and only the secret masked."""
    from apm_suite import runner

    rc = runner.run(
        [sys.executable, "-c", "pass", "--telnet-password", "sekret", "--other", "value"],
    )
    assert rc == 0
    echoed = capsys.readouterr().out
    assert "sekret" not in echoed
    assert "<redacted>" in echoed
    assert "--other value" in echoed


def _boom_spec() -> CollectorSpec:
    return CollectorSpec(
        name="boom",
        layer="threads",
        artifact="threads/out.jsonl",
        tool="sh",  # must pass the shutil.which gate on any Linux host
        build=lambda ctx: [sys.executable, "-c", "pass"],
        stdout_to="threads/out.txt",
    )


def test_launch_collectors_closes_opened_streams_on_open_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError between the stdout open and Popen (e.g. stderr path is a
    directory) must close the already-opened descriptor and record a failed
    result instead of leaking an fd and aborting the launch pipeline."""
    (tmp_path / "threads").mkdir()
    (tmp_path / "threads" / "boom.err").mkdir()  # IsADirectoryError on stderr open
    opened: list[Any] = []
    real_open = Path.open

    def tracking_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, mode, *args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(capture, "SPECS", (_boom_spec(),))
    ctx = CaptureContext(session=tmp_path, pid=os.getpid(), comm="test", seconds=1)
    running: list[capture._Running] = []

    capture._launch_collectors(ctx, "boom", False, running)

    assert running == []
    assert opened, "expected at least one tracked stream to be opened"
    assert all(handle.closed for handle in opened)
    result = json.loads((tmp_path / "threads" / "boom.result.json").read_text())
    assert result["status"] == "failed"


# --- unit: identity + retention policy --------------------------------------------


def test_claim_dir_returns_fresh_directory_per_call(tmp_path: Path) -> None:
    """Claiming is exclusive creation: every call returns its own directory even
    when nothing has been written into the earlier ones yet (the probe-then-create
    variant let two same-second runs both 'win' the free path and interleave
    their evidence in one session)."""
    from apm_suite.io import claim_dir

    base = tmp_path / "session_20260101_000000_pid1"
    assert claim_dir(base) == base
    assert base.is_dir()  # claimed = exists immediately, before any content
    assert claim_dir(base) == tmp_path / f"{base.name}_1"
    assert (tmp_path / f"{base.name}_1").is_dir()
    assert claim_dir(base) == tmp_path / f"{base.name}_2"


def test_claim_file_creates_exclusive_empty_marker(tmp_path: Path) -> None:
    from apm_suite.io import claim_file

    base = tmp_path / "nested" / "loadgen_123.json"
    assert claim_file(base) == base
    assert base.is_file() and base.read_bytes() == b""
    assert claim_file(base) == tmp_path / "nested" / f"{base.name}_1"
    # A directory squatting on the name forces the next suffix too.
    blocked = tmp_path / "nested" / "blocked.json"
    blocked.mkdir()
    assert claim_file(blocked) == tmp_path / "nested" / f"{blocked.name}_1"


def test_retention_policy_shared_by_prune_and_auto_prune(tmp_path: Path) -> None:
    """One retention implementation feeds the CLI prune command and post-capture
    auto-prune: keep-N ordering and the size budget must behave identically."""
    from apm_suite.session import list_sessions, sessions_beyond_budget

    names = [f"session_{i:03d}" for i in range(5)]
    for i, name in enumerate(names):
        path = tmp_path / name
        path.mkdir()
        (path / "data.bin").write_bytes(b"x" * (10 + i))
        os.utime(path, (1000 + i, 1000 + i))  # oldest first

    sessions = list_sessions(tmp_path)
    assert [s.name for s in sessions] == list(reversed(names))  # newest first

    assert sessions_beyond_budget(sessions, 2) == [
        tmp_path / "session_002",
        tmp_path / "session_001",
        tmp_path / "session_000",
    ]

    # Size budget: keep=5 retains everything under a generous cap...
    assert sessions_beyond_budget(sessions, 5, max_bytes=1024**3) == []
    # ...and evicts oldest kept first until the total fits a tight one
    # (slice already removes 000/001; the 30-byte cap then also drops 002).
    doomed = sessions_beyond_budget(sessions, 3, max_bytes=30)
    assert doomed == [
        tmp_path / "session_001",
        tmp_path / "session_000",
        tmp_path / "session_002",  # oldest kept session goes first under budget
    ]


def test_list_sessions_breaks_mtime_ties_by_name(tmp_path: Path) -> None:
    """Equal mtimes (same-second captures, restored archives) must order by
    name: a readdir-order tie would make prune pick different victims per run."""
    from apm_suite.session import list_sessions

    for name in ("session_b", "session_a", "session_c"):
        (tmp_path / name).mkdir()
        os.utime(tmp_path / name, (5000, 5000))
    assert [p.name for p in list_sessions(tmp_path)] == [
        "session_c",
        "session_b",
        "session_a",
    ]


def test_scenario_runs_expire_on_the_prune_clock(tmp_path: Path) -> None:
    """`scenario run` leaves one manifest + stats pair per invocation under
    .scenario; nothing else ever deletes them, so periodic captures on a 24/7
    host would accumulate files forever. The purge must follow the shared
    grace clock, touch only the loadgen_* family, and report failures."""
    from apm_suite.session import purge_stale_scenario_runs

    scenario = tmp_path / ".scenario"
    scenario.mkdir()
    stale = scenario / "loadgen_1000.json"
    stale_stats = scenario / "loadgen_1000_stats.json"
    fresh = scenario / "loadgen_9000.json"
    foreign = scenario / "exp1_workload.json"
    for path in (stale, stale_stats, fresh, foreign):
        path.write_text("{}")
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))
    os.utime(stale_stats, (old, old))

    removed = {entry.name for entry, error in purge_stale_scenario_runs(tmp_path, 24.0)}
    assert removed == {"loadgen_1000.json", "loadgen_1000_stats.json"}
    assert not stale.exists() and not stale_stats.exists()
    assert fresh.exists() and foreign.exists()

    # Grace 0 hard-deletes everything whose mtime precedes the purge call.
    removed = {entry.name for entry, error in purge_stale_scenario_runs(tmp_path, 0.0)}
    assert removed == {"loadgen_9000.json"}
    assert not fresh.exists()

    # Missing .scenario dir is a no-op, not an error.
    empty = tmp_path / "nowhere"
    empty.mkdir()
    assert list(purge_stale_scenario_runs(empty)) == []


class _FrozenClock:
    """Stands in for capture.datetime so two runs share one wall-clock stamp."""

    @staticmethod
    def now(_tz: object | None = None) -> datetime:
        return datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_run_capture_gives_same_stamp_captures_distinct_session_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two captures with an identical wall-clock stamp (retry in the same second,
    cron overlap) must each claim their own session directory instead of
    interleaving collectors' evidence; the returned dir must be the one actually
    written to (meta.json lands inside it)."""
    root = tmp_path / "apm"
    root.mkdir()
    monkeypatch.setattr(capture, "datetime", _FrozenClock)
    monkeypatch.setattr(capture, "apm_root", lambda: root)
    monkeypatch.setattr(capture, "_sudo_available", lambda: False)
    monkeypatch.setattr(capture, "tool_version", lambda name: "")

    def capture_once() -> Path:
        outcome = capture.run_capture(
            seconds=1,
            pid=os.getpid(),
            only="",  # no collectors requested: this run pins dir claiming alone
            no_app=True,
            telnet_host="",
            telnet_port=0,
            telnet_password="",
            finalize=False,
            symbolize=False,
            reset_bridge=False,
        )
        return outcome.session

    first = capture_once()
    second = capture_once()

    base = f"session_20260101_000000_pid{os.getpid()}"
    assert first == root / base  # free path won by exclusive creation
    assert second == root / f"{base}_1"  # collision resolved, not shared
    assert first.is_dir() and second.is_dir()
    for session in (first, second):
        assert load_json(session / "meta.json")["pid"] == os.getpid()


def test_path_env_overrides_treat_empty_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exported-but-empty override must not collapse to Path("") == cwd,
    which would point the session store (and its prune scans) at the repo."""
    from apm_suite import paths

    monkeypatch.setenv("SEVENDTD_APM_DIR", "")
    monkeypatch.setenv("SEVENDTD_DS_DIR", " ")
    # whitespace-only counts as unset too
    assert paths.apm_root() == Path.home() / ".local/share/7dtd-server-apm"
    assert paths.dedicated_dir() == paths.DEFAULT_DS

    monkeypatch.setenv("SEVENDTD_APM_DIR", str(tmp_path))
    monkeypatch.setenv("SEVENDTD_DS_DIR", str(tmp_path / "ds"))
    assert paths.apm_root() == tmp_path
    assert paths.dedicated_dir() == tmp_path / "ds"


def test_retention_env_values_warn_and_fall_back_on_garbage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd retention value must warn instead of silently pretending the
    operator's setting was read (auto-prune deleting evidence is destructive)."""
    from apm_suite.session import keep_sessions_budget, prune_grace_hours

    monkeypatch.delenv("APM_KEEP_SESSIONS", raising=False)
    monkeypatch.delenv("APM_PRUNE_GRACE_HOURS", raising=False)
    assert keep_sessions_budget() == 40
    assert prune_grace_hours() == 24.0
    assert capsys.readouterr().err == ""

    # Explicit values are honored, including the documented opt-outs.
    monkeypatch.setenv("APM_KEEP_SESSIONS", "0")
    monkeypatch.setenv("APM_PRUNE_GRACE_HOURS", "0")
    assert keep_sessions_budget() == 0
    assert prune_grace_hours() == 0.0
    assert capsys.readouterr().err == ""

    # Empty = unset (no warning); garbage warns and uses the default.
    for name, getter, default in (
        ("APM_KEEP_SESSIONS", keep_sessions_budget, 40),
        ("APM_PRUNE_GRACE_HOURS", prune_grace_hours, 24.0),
    ):
        monkeypatch.setenv(name, "")
        assert getter() == default
        monkeypatch.setenv(name, "not-a-number")
        assert getter() == default
        assert "WARNING" in capsys.readouterr().err


def test_telnet_password_warning_scopes_to_app_layer_requests() -> None:
    """Missing-password warning fires only when the app collector will run."""
    message = capture._telnet_password_warning("all", no_app=False, telnet_password="")
    assert message is not None and "SEVENDTD_TELNET_PASSWORD" in message
    assert capture._telnet_password_warning("app", False, "") is not None
    assert capture._telnet_password_warning("cpu,memory", False, "") is None
    assert capture._telnet_password_warning("all", no_app=True, telnet_password="") is None
    assert capture._telnet_password_warning("all", False, telnet_password="pw") is None


# --- unit: telnet cohort helpers ---------------------------------------------------


_LISTPLAYERS = (
    "2 players online. Send 'help' for commands.\n"
    "1. id=171, name=Alice, pos=(100.0, 63.0, -200.25)\n"
    "2. id=172, name=Bob, pos=(10.5, 60.0, 20.0)\n"
    "3. id=173, name=Cid, pos=(-5.0, 61.0, 30.0)\n"
)


def _rally(
    monkeypatch: pytest.MonkeyPatch, listing: str, at: tuple[int, int] | None = None
) -> tuple[int, list[str]]:
    commands: list[str] = []

    def fake_telnet_exec(*_args: object) -> str:
        return listing

    def fake_telnet_command(_h: str, _p: int, _pw: str, command: str) -> bool:
        commands.append(command)
        return True

    monkeypatch.setattr(capture, "telnet_exec", fake_telnet_exec)
    monkeypatch.setattr(capture, "telnet_command", fake_telnet_command)
    return capture.rally_players("127.0.0.1", 8081, "pw", at=at), commands


def test_rally_players_clusters_cohort_around_first_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --rally-at the FIRST listed player anchors the cluster (y kept),
    the rest are teleported into a grid around them, and the moved count covers
    exactly the non-anchor players. The regex silently matches nothing when the
    game's listplayers format shifts, so the emitted commands are pinned here."""
    moved, commands = _rally(monkeypatch, _LISTPLAYERS)
    assert moved == 2
    assert commands == [
        "teleportplayer 172 85 63 -215",
        "teleportplayer 173 91 63 -215",
    ]


def test_rally_players_rally_at_anchors_everyone_to_fresh_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --rally-at every player including the first moves into the grid at
    the fresh coordinates, with y=-1 so the server finds ground."""
    moved, commands = _rally(monkeypatch, _LISTPLAYERS, at=(500, 900))
    assert moved == 3
    assert commands == [
        "teleportplayer 171 485 -1 885",
        "teleportplayer 172 491 -1 885",
        "teleportplayer 173 497 -1 885",
    ]


@pytest.mark.parametrize(
    "listing",
    [
        "",  # server unreachable / empty reply
        "no players connected\n",  # header only, no rows
        "1. id=171, name=Solo, pos=(1.0, 2.0, 3.0)\n",  # single player: nowhere to rally to
        "1. id=x, name=Broken, position unknown\n",  # format drift: regex must not guess
    ],
)
def test_rally_players_moves_nobody_without_parseable_positions(
    monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    moved, commands = _rally(monkeypatch, listing)
    assert moved == 0
    assert commands == []


def test_doctor_reports_resolved_environment_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """doctor exposes the active configuration so misread overrides surface;
    the telnet secret must appear only as set/unset, never its value."""
    from apm_suite.doctor import inspect

    monkeypatch.setenv("SEVENDTD_APM_DIR", str(tmp_path))
    monkeypatch.setenv("APM_PRUNE_GRACE_HOURS", "6")
    monkeypatch.delenv("SEVENDTD_TELNET_PASSWORD", raising=False)
    result = inspect(None, "127.0.0.1", 8081)
    env = result["environment"]
    assert env["apm_root"] == str(tmp_path)
    assert env["prune_grace_hours"] == 6.0
    assert env["keep_sessions"] == 40
    assert env["telnet_password_set"] is False
    assert "apm-root-secret" not in json.dumps(result)

    monkeypatch.setenv("SEVENDTD_TELNET_PASSWORD", "apm-root-secret")
    assert inspect(None, "127.0.0.1", 8081)["environment"]["telnet_password_set"] is True


def test_doctor_sudo_timeout_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sudo check's timeout exists for a hung sudo; hitting it must produce
    a failed check, not crash the whole doctor report with TimeoutExpired."""
    import subprocess

    from apm_suite.doctor import _sudo

    monkeypatch.setattr("apm_suite.doctor.shutil.which", lambda name: "/usr/bin/sudo")

    def fake_run(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="sudo", timeout=3)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _sudo()
    assert result["ok"] is False
    assert "timed out" in (result["fix"] or "")


def test_bind_mono_reports_the_precise_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each None return carries its own reason in WARN.txt: missing library,
    missing sudo, and failed/hung mount are fixed differently, and the old
    single generic message hid which one applied."""
    import subprocess

    from apm_suite import capture

    session = tmp_path / "session"
    session.mkdir()

    monkeypatch.setattr(capture, "_mono_library", lambda pid: None)
    assert capture._bind_mono(session, 1, sudo_ok=True) is None
    assert "not mapped" in (session / "WARN.txt").read_text()

    source = tmp_path / "libmonobdwgc-2.0.so"
    source.write_bytes(b"x")
    monkeypatch.setattr(capture, "_mono_library", lambda pid: source)
    assert capture._bind_mono(session, 1, sudo_ok=False) is None
    assert "sudo -n unavailable" in (session / "WARN.txt").read_text()

    def fake_run(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="sudo", timeout=15)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert capture._bind_mono(session, 1, sudo_ok=True) is None
    assert "bind mount failed" in (session / "WARN.txt").read_text()


def test_capture_sudo_probe_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung `sudo -n true` probe must read as unavailable instead of hanging
    every capture at startup before a collector launches."""
    import shutil
    import subprocess as sp

    def fake_run(*args: object, **kwargs: object) -> object:
        timeout = float(kwargs.get("timeout") or 0)  # type: ignore[arg-type]
        assert timeout > 0, "sudo probe must pass a timeout"
        raise sp.TimeoutExpired(cmd="sudo", timeout=timeout)

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(sp, "run", fake_run)
    assert capture._sudo_available() is False


# --- error-path surfacing ---------------------------------------------------


def _squashed(text: str) -> str:
    """Console output wraps mid-word at terminal width and Typer's rich error
    panels add '│' gutters; containment checks run against both removed."""
    return "".join(text.split()).replace("│", "")


def test_rally_players_failed_teleports_are_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A telnet send failure must shrink the moved count instead of reporting
    phantom rallies (an unrallied cohort presented as a valid cluster)."""
    commands: list[str] = []

    def fake_telnet_exec(*_args: object) -> str:
        return _LISTPLAYERS

    def fake_telnet_command(_h: str, _p: int, _pw: str, command: str) -> bool:
        commands.append(command)
        return False

    monkeypatch.setattr(capture, "telnet_exec", fake_telnet_exec)
    monkeypatch.setattr(capture, "telnet_command", fake_telnet_command)
    moved = capture.rally_players("127.0.0.1", 8081, "pw", at=(500, 900))
    assert moved == 0
    assert len(commands) == 3  # every player was still attempted


def test_warn_survives_unwritable_warn_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """_warn runs inside error handlers: an unwritable WARN.txt must reach the
    operator on stderr, never abort the flow that was reporting a problem."""
    (tmp_path / "WARN.txt").mkdir()
    capture._warn(tmp_path, "probe message")
    err = capsys.readouterr().err
    assert "WARN: probe message" in err
    assert "could not append" in err


def test_budget_names_corrupt_bridge_file(tmp_path: Path) -> None:
    """A torn csharp_bridge.json must fail the gate naming the file, not with a
    bare 'Expecting value' that leaves the operator guessing which artifact."""
    session = _session(tmp_path / "session_corrupt_bridge")
    (session / "csharp_bridge.json").write_text('{"top_managed_sections": ')
    result = runner.invoke(app, ["budget", str(session)])
    assert result.exit_code == 2
    squashed = _squashed(result.stderr)
    assert str(session / "csharp_bridge.json") in squashed
    assert "cannotparse" in squashed


def test_compare_names_corrupt_bridge_file(tmp_path: Path) -> None:
    base = _cmp_session(tmp_path, "cmp_base")
    candidate = _cmp_session(tmp_path, "cmp_cand")
    (candidate / "csharp_bridge.json").write_text("[torn")
    result = runner.invoke(app, ["compare", str(base), str(candidate)])
    assert result.exit_code == 1
    squashed = _squashed(result.stderr)
    assert "cannotparse" in squashed
    assert str(candidate / "csharp_bridge.json") in squashed


def test_prometheus_rejects_corrupt_health_json_cleanly(tmp_path: Path) -> None:
    """A hand-mangled health.json must produce a named-path CLI error like the
    guarded summary read above it, never a raw traceback."""
    session = _session(tmp_path / "session_corrupt_health")
    (session / "health.json").write_text("{oops")
    out = tmp_path / "metrics.txt"
    result = runner.invoke(app, ["prometheus", str(session), "--output", str(out)])
    assert result.exit_code == 2
    assert str(session / "health.json") in _squashed(result.stderr)


def test_ingest_bridge_snapshot_copy_failure_warns_not_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed snapshot copy (disk full, perms) degrades to WARN.txt; the
    post-capture pipeline (finalize/audit/prune) still runs."""
    from datetime import UTC
    from datetime import datetime as dt

    pid = 4242
    exe = tmp_path / "server"
    telemetry = exe.parent / "Mods/7dtd-server-apm-bridge/telemetry"
    telemetry.mkdir(parents=True)
    # The capture window opens before the snapshot is stamped, else the
    # freshness gate rejects the file before the copy is ever attempted.
    started = dt.now(UTC)
    snapshot = {
        "schema": "7dtd.apm.app.v3",
        "provider": "7dtd-server-apm-bridge",
        "providerVersion": "0.0.0",
        "utc": dt.now(UTC).isoformat(),
        "sections": [
            {
                "name": "World.TickEntities",
                "calls": 10,
                "avgMs": 1.0,
                "lastMs": 1.0,
                "maxMs": 2.0,
                "p50Ms": 1.0,
                "p95Ms": 2.0,
                "p99Ms": 2.0,
                "totalMs": 10.0,
            }
        ],
    }
    (telemetry / "apm_app_latest.json").write_text(json.dumps(snapshot))

    real_realpath = os.path.realpath

    def fake_realpath(path: object) -> str:
        return str(exe) if str(path) == f"/proc/{pid}/exe" else real_realpath(str(path))

    monkeypatch.setattr("apm_suite.capture.os.path.realpath", fake_realpath)

    def failing_copy2(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("apm_suite.capture.shutil.copy2", failing_copy2)

    session = tmp_path / "session"
    (session / "app").mkdir(parents=True)
    capture._ingest_bridge_snapshot(session, pid, False, started, 60)
    err = capsys.readouterr().err
    assert "copy failed" in err
    assert "not ingested" in (session / "WARN.txt").read_text()


def test_scaling_skips_unreadable_summaries_like_missing_ones(tmp_path: Path) -> None:
    """One torn summary must not crash the ladder fit; it is dropped exactly
    like a session without summary.json and the rest still fit."""
    from apm_suite.analysis.scaling import analyze_scaling

    sessions: list[Path] = []
    for index, players in enumerate((5, 10, 20)):
        session = tmp_path / f"scale_{index}"
        session.mkdir()
        meta = _meta(seconds=30)
        meta["analyzer_version"] = "2.1.0"
        atomic_json(session / "meta.json", meta)
        summary = _summary(session.name, [{"layer": "cpu", "score": 10, "state": "collected"}])
        summary["metadata"] = {"world": {"entities": players * 100, "players": players}}
        atomic_json(session / "summary.json", summary)
        sessions.append(session)
    (sessions[1] / "summary.json").write_text("{torn")

    result = analyze_scaling(sessions, scale_key="players")
    assert result["scales"] == [5.0, 20.0]  # torn session excluded, fit survives


# --- live server (opt-in) ----------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("SEVENDTD_LIVE"),
    reason="set SEVENDTD_LIVE=1 with a running dedicated server to enable",
)
def test_live_server_doctor_reports_target() -> None:
    from apm_suite.doctor import inspect

    result = inspect(None, "127.0.0.1", 8081)
    assert result["schema"] == "7dtd.apm.doctor.v2"
    assert result["checks"]["target"]["ok"], "server process not found"
