from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from apm_suite.analysis.bridge import match_rules, parse_section_line
from apm_suite.analysis.events import PER_SOURCE_MAX, build_timeline
from apm_suite.analysis.health import build_health
from apm_suite.analysis.report import parse_perf_stat, top_alloc_sites
from apm_suite.cli import app
from apm_suite.finalize import finalize
from apm_suite.io import atomic_json, load_json
from apm_suite.models import LayerScore, ManifestV2, Target, schema_dict
from apm_suite.reporting import render_session
from apm_suite.session import REQUIRED, audit_session
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
    assert sites == ["AstarVoxelGrid.InitScan", "NetEntityDistribution.OnUpdateEntities", "GameTimer.Reset"]
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
    assert out["meta"]["cmdline"] == "<redacted>"
    assert out["meta"]["exe"] == "<redacted>"
    assert out["meta"]["pid"] == 42  # non-sensitive fields preserved


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
    assert result.exit_code != 0


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
