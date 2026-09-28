from __future__ import annotations

import inspect
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from apm_suite import capture, paths
from apm_suite.analysis.bridge import (
    layer_state,
    match_rules,
    parse_section_line,
    ranked_section_heats,
)
from apm_suite.analysis.events import PER_SOURCE_MAX, build_timeline
from apm_suite.analysis.health import build_health
from apm_suite.analysis.report import parse_perf_stat, top_alloc_sites
from apm_suite.analysis.scaling import analyze_scaling
from apm_suite.capture import CaptureContext, CollectorSpec
from apm_suite.cli import app, console
from apm_suite.finalize import finalize
from apm_suite.io import atomic_json, force_utf8_stdio, load_json, load_jsonc, write_stdout
from apm_suite.models import (
    Artifact,
    EventsV2,
    LayerScore,
    ManifestV2,
    Target,
    object_list,
    schema_dict,
)
from apm_suite.paths import REPO
from apm_suite.reporting import render_session
from apm_suite.runner import terminate_tree
from apm_suite.session import REQUIRED, audit_session

runner = CliRunner()

FIXTURES = Path(__file__).with_name("fixtures")


def test_repo_root_resolves_to_the_checkout_marker() -> None:
    """paths.REPO walks up for the (pyproject.toml, tools/apm_suite) marker
    instead of counting parents. A wrong answer silently repoints every
    collector backend, bpftrace source, and script path derived from it."""
    assert (REPO / "pyproject.toml").is_file()
    assert (REPO / "tools/apm_suite").is_dir()
    assert Path(__file__).resolve().is_relative_to(REPO)


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
    from typer.main import get_command

    from apm_suite import __version__

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


def test_cli_output_target_failures_are_clean_not_tracebacks(tmp_path: Path) -> None:
    """An unusable --output/--json destination is an operator error naming the
    flag: exit 2 on stderr, never a rich traceback with source frames."""
    session = _session(tmp_path / "session_out")
    as_dir = tmp_path / "a-directory"
    as_dir.mkdir()
    cases: list[list[str]] = [
        ["export", str(session), "--output", str(as_dir)],
        ["prometheus", str(session), "--output", str(as_dir)],
        ["doctor", "--json", str(as_dir)],
    ]
    for argv in cases:
        result = runner.invoke(app, argv)
        assert result.exit_code == 2, argv
        assert result.stdout == "", f"error leaked to stdout: {argv}"
        assert "Traceback" not in result.stderr, argv
        assert "[red]" not in result.stderr, argv
        assert "mustnotbeadirectory" in _squashed(result.stderr), argv
        # The flag at fault is named, so the fix does not require a bisect.
        assert any(flag in result.stderr for flag in ("--output", "--json")), argv


def test_cli_index_takes_the_shared_store_flag(tmp_path: Path) -> None:
    """index, import, and verify-store all name the same directory --store;
    --root stays accepted as the spelling index shipped."""
    store = tmp_path / "store"
    for flag in ("--store", "--root"):
        result = runner.invoke(app, ["index", flag, str(store)])
        assert result.exit_code == 0, result.output
        assert (store / "index.html").is_file()


@pytest.mark.skipif(sys.stdout.isatty(), reason="help is rich-formatted at a terminal")
def test_cli_help_is_plain_text_when_stdout_is_redirected() -> None:
    """Piped help must stay greppable: no box drawing, no padded columns."""
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0
    assert "╭" not in result.output and "│" not in result.output
    assert "Usage: root capture" in result.output
    assert "--seconds" in result.output
    assert all(line == line.rstrip() for line in result.output.splitlines())


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


def test_thread_summary_averages_across_samples_and_folds_streaming(
    tmp_path: Path,
) -> None:
    """The summary folds the file instead of materializing every sample: only
    samples whose process total is positive enter both running means, a
    non-list "top" row contributes nothing, and the LAST intact record wins
    for n_threads/wchan_top. A JSON-valid but non-object line (corrupt writer)
    is dropped like every other jsonl reader here instead of crashing."""
    from apm_suite.analysis.report import thread_summary

    session = tmp_path / "session_fold"
    (session / "threads").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta(pid=5))

    def sample(t: float, top: object, threads: int) -> str:
        return json.dumps({"t": t, "n_threads": threads, "top": top})

    lines = [
        sample(1.0, [{"tid": 5, "cpu_pct": 20.0}, {"tid": 6, "cpu_pct": 60.0}], 9),
        "[not,an,object]",  # valid JSON, wrong shape: skipped entirely
        sample(2.0, "not-a-list", 8),  # unparsable top row: no contribution
        sample(3.0, [{"tid": 5, "cpu_pct": 50.0}], 7),
        sample(4.0, [{"tid": 6, "cpu_pct": 0.0}], 6),  # total == 0: not averaged
    ]
    (session / "threads/threads.jsonl").write_text("\n".join(lines) + "\n")
    summary = thread_summary(session)
    # main cpu: (20 + 50) / 2 averaged samples
    assert summary["main_thread_cpu_pct_avg"] == 35.0
    # main share of process: (20/80 + 50/50) / 2
    assert summary["main_thread_share_of_process_avg"] == pytest.approx(0.625)
    # last intact record, not the highest-t one that contributed
    assert summary["n_threads"] == 6


def test_thread_summary_share_uses_whole_process_total(tmp_path: Path) -> None:
    """The persisted top list is capped (--top), so its row sum is NOT the
    process total: a record carrying the collector's whole-process CPU% must
    use it as the share denominator, or the main thread's share of process CPU
    is overstated whenever more threads than the cap carry CPU (main=80 of a
    top-15 sum of 150 reads 53%, but is 35% of the real process load)."""
    from apm_suite.analysis.report import thread_summary

    session = tmp_path / "session_process_total"
    (session / "threads").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta(pid=5))
    rows = [{"tid": 5, "cpu_pct": 80.0}] + [{"tid": 100 + i, "cpu_pct": 5.0} for i in range(14)]
    row = json.dumps({"t": 1.0, "n_threads": 31, "top": rows, "process_cpu_pct": 230.0})
    (session / "threads/threads.jsonl").write_text(row + "\n")
    summary = thread_summary(session)
    assert summary["main_thread_cpu_pct_avg"] == 80.0
    assert summary["main_thread_share_of_process_avg"] == pytest.approx(round(80.0 / 230.0, 3))


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


# --- scenario run orchestration ---------------------------------------------------
#
# `scenario run` is fully hermetic here: the sibling loadgen script, the loadgen
# process, and run_capture are all faked, because a real invocation would spawn
# the actual bot cohort found on development hosts.


class _FakeLoadgenProcess:
    def __init__(self, returncode: int) -> None:
        self.pid = 424242
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class _CaptureOutcome:
    def __init__(self, session: Path, exit_code: int) -> None:
        self.session = session
        self.exit_code = exit_code


def _fake_loadgen_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A sibling loadgen checkout with a no-op runner, plus an empty APM store.

    Returns (repo, store): the repo stands in for the checkout cli.REPO points
    at, the store for the apm_root the scenario command writes under.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    loadgen = tmp_path / "7dtd-loadgen" / "scripts" / "run_loadgen.sh"
    loadgen.parent.mkdir(parents=True)
    loadgen.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    store = tmp_path / "store"
    store.mkdir()
    return repo, store


def _scenario_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest_body: str | None = None,
    stats_body: str | None = None,
    capture_rc: int = 0,
    loadgen_rc: int = 0,
) -> tuple[Path, list[str], list[dict[str, object]], Path]:
    """Isolate `scenario run` behind fakes; returns (session, loadgen argvs,
    captured run_capture kwargs, store root). The run_capture stub also plays
    the loadgen writing its manifest + stats beside the claimed path."""
    import apm_suite.cli as cli_module

    repo, store = _fake_loadgen_tree(tmp_path)

    started: list[str] = []
    captured: list[dict[str, object]] = []

    def fake_popen(argv: list[str], **_kwargs: object) -> _FakeLoadgenProcess:
        started.append(str(argv[0]))
        return _FakeLoadgenProcess(loadgen_rc)

    monkeypatch.setattr(
        cli_module,
        "subprocess",
        SimpleNamespace(Popen=fake_popen, TimeoutExpired=subprocess.TimeoutExpired),
    )
    monkeypatch.setattr(cli_module, "REPO", repo)
    monkeypatch.setattr(cli_module, "apm_root", lambda: store)

    session = _session(tmp_path / "session_scn")

    def fake_run_capture(**kwargs: object) -> _CaptureOutcome:
        captured.append(kwargs)
        if manifest_body is not None:
            claimed = sorted((store / ".scenario").glob("loadgen_*.json"))
            assert len(claimed) == 1
            claimed[0].write_text(manifest_body, encoding="utf-8")
            if stats_body is not None:
                claimed[0].with_name(f"{claimed[0].stem}_stats.json").write_text(
                    stats_body, encoding="utf-8"
                )
        return _CaptureOutcome(session, capture_rc)

    monkeypatch.setattr(cli_module, "run_capture", fake_run_capture)
    return session, started, captured, store


def test_scenario_run_rejects_unknown_preset_before_any_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preset gate runs first: nothing spawned, no manifest claimed."""
    _, started, _, store = _scenario_env(tmp_path, monkeypatch)
    result = runner.invoke(app, ["scenario", "run", "--preset", "bogus"])
    assert result.exit_code == 2
    # Typer's enum parser rejects the value; the choices stay discoverable.
    squashed = _squashed(result.stderr)
    assert "bogus" in squashed
    for choice in ("standard", "deep", "forensic"):
        assert choice in squashed
    assert started == []
    assert not (store / ".scenario").exists()


def test_scenario_run_names_missing_sibling_loadgen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing sibling tree must fail with the resolved path instead of a
    FileNotFoundError from Popen. The real sibling exists on dev hosts, so the
    lookup is pointed at an empty repo root for hermeticity."""
    import apm_suite.cli as cli_module

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(cli_module, "REPO", tmp_path / "empty_repo")
    monkeypatch.setattr(cli_module, "apm_root", lambda: store)
    result = runner.invoke(app, ["scenario", "run"], env={"COLUMNS": "4096"})
    assert result.exit_code == 2
    expected = (tmp_path / "empty_repo").parent / "7dtd-loadgen/scripts/run_loadgen.sh"
    assert str(expected) in result.stderr
    assert "sibling load generator not found" in result.stderr
    assert not (store / ".scenario").exists()


def test_scenario_run_validates_rally_at_before_starting_loadgen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd --rally-at must fail fast with no leaked subprocess and no
    wasted warmup (documented ordering in scenario_run)."""
    _, started, _, _store = _scenario_env(tmp_path, monkeypatch)
    result = runner.invoke(app, ["scenario", "run", "--rally-at", "north"])
    assert result.exit_code == 2
    squashed = _squashed(result.stderr)
    # The rich error panel wraps mid-token, so match the tail of the message.
    assert "expects'x,z'" in squashed
    assert "'north'" in squashed
    assert started == []


def test_scenario_run_attaches_workload_manifest_and_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attach step must copy the loadgen-written manifest into the session
    with the label and botMode default recorded, copy the stats file, and audit
    the resulting evidence; the default preset maps to its documented collectors."""
    session, started, captured, _store = _scenario_env(
        tmp_path,
        monkeypatch,
        manifest_body='{"mode": "clients", "target": "standard", "seed": 7}',
        stats_body='{"actions_done": 500}\n',
    )
    result = runner.invoke(
        app,
        ["scenario", "run", "--seconds", "5", "--label", "perf-fix"],
        env={"COLUMNS": "4096"},
    )
    assert result.exit_code == 0, result.output
    assert len(started) == 1  # exactly one loadgen launch
    assert captured[0]["only"] == "app,threads,memory,cpu"  # standard preset table
    workload = load_json(session / "workload.json")
    assert workload["label"] == "perf-fix"
    assert workload["mode"] == "clients"
    assert workload["workload"]["botMode"] == "auto"  # recorded default, not absent
    assert (session / "loadgen_stats.json").read_text() == '{"actions_done": 500}\n'
    # The post-capture audit ran over the attached evidence.
    assert load_json(session / "manifest.json")["schema"] == "7dtd.apm.manifest.v2"
    assert "workload manifest attached" in result.stdout


@pytest.mark.parametrize(
    "body,message",
    [
        ('{"mode": ', "loadgen manifest unreadable, not attached"),
        ("[1, 2]", "loadgen manifest is not a JSON object, not attached"),
    ],
)
def test_scenario_run_survives_bad_loadgen_manifest_and_still_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, message: str
) -> None:
    """A torn or non-object manifest write (loadgen killed mid-flush) must not
    crash the attach after the capture succeeded: report it, attach nothing,
    and still audit the session evidence."""
    session, _started, _captured, _store = _scenario_env(tmp_path, monkeypatch, manifest_body=body)
    result = runner.invoke(app, ["scenario", "run"], env={"COLUMNS": "4096"})
    assert result.exit_code == 0, result.output  # the capture itself succeeded
    assert message in result.stderr
    assert not (session / "workload.json").exists()
    assert (session / "manifest.json").is_file()


def test_scenario_run_reports_unreadable_stats_and_still_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stats file that cannot be copied (perms, vanished) must be reported,
    not raised: the capture already succeeded and its evidence is on disk, so a
    traceback here would skip the audit and the matrix exit code."""
    import shutil

    session, _started, _captured, _store = _scenario_env(
        tmp_path,
        monkeypatch,
        manifest_body='{"mode": "clients"}',
        stats_body='{"actions_done": 500}\n',
    )
    real_copy2 = shutil.copy2

    def deny(src: Any, dst: Any, **kwargs: Any) -> Any:
        if str(dst).endswith("loadgen_stats.json"):
            raise PermissionError(13, "Permission denied")
        return real_copy2(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copy2", deny)
    result = runner.invoke(app, ["scenario", "run"], env={"COLUMNS": "4096"})
    assert result.exit_code == 0, result.output
    assert "loadgen stats not attached" in result.stderr
    assert not (session / "loadgen_stats.json").exists()
    assert (session / "manifest.json").is_file()  # the audit still ran


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


def test_stdout_stays_utf8_under_a_c_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A C locale must not turn a non-ASCII path or hostname into a crash.

    Rich writes straight to the text stream and does not guard, so without
    the CLI's encoding pin the first non-ASCII character a command prints
    raises UnicodeEncodeError and the operator sees a traceback instead of
    the report.
    """
    out_buffer = io.BytesIO()
    err_buffer = io.BytesIO()
    out = io.TextIOWrapper(out_buffer, encoding="ascii", errors="strict")
    err = io.TextIOWrapper(err_buffer, encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    force_utf8_stdio()
    session = tmp_path / "seßion_1"
    console.print(f"session: {session}")
    write_stdout("café\n")
    out.flush()

    written = out_buffer.getvalue().decode("utf-8")
    assert f"{session}\n" in written
    assert written.endswith("café\n")


def test_the_typer_callback_runs_the_encoding_pin() -> None:
    """The app must have exactly one callback, and it must pin the streams.

    Typer keeps a single registered callback slot and a second @app.callback()
    overwrites the first, so a pin parked on its own decorator is dead code
    that reads exactly like a live entry point. This asserts the pin is
    reachable from the registered callback every command actually goes through.
    """
    from apm_suite import cli as cli_module

    assert cli_module.app.registered_callback is not None
    assert cli_module.app.registered_callback.callback is cli_module.root
    source = inspect.getsource(cli_module.root)
    assert "force_utf8_stdio" in source, "the root callback no longer pins stdio"


def test_doctor_json_stdout_is_machine_readable() -> None:
    result = runner.invoke(app, ["doctor", "--json", "-"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["schema"].startswith("7dtd.apm.doctor.v2")


def test_bridge_status_tolerates_non_object_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edited apmbridge.json holding valid-but-non-object JSON ("[1,2]")
    is a diagnosable condition: doctor must report, not crash with
    AttributeError out of settings.get (the whole-report posture every other
    malformed input in _bridge_status follows)."""
    from apm_suite import doctor

    mods = tmp_path / "Mods/7dtd-server-apm-bridge"
    (mods / "Config").mkdir(parents=True)
    (mods / "7dtd-server-apm-bridge.dll").write_bytes(b"dll")
    # _bridge_status resolves the mod folder through paths.bridge_mod_dir, so
    # patch that boundary (not dedicated_dir) to point it at the fake install.
    monkeypatch.setattr(doctor, "bridge_mod_dir", lambda: mods)
    # Isolate REPO so _bridge_status cannot compare against a real dist build
    # from the working tree; this test pins the malformed-config posture, not
    # the stale-DLL verdict.
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    for body in ("[1, 2]", '"DeepMode"', "42", "null", ""):
        (mods / "Config/apmbridge.json").write_text(body)
        status = doctor._bridge_status()
        assert status["ok"] is True
        assert "deep_mode" not in status


def test_doctor_prints_deepmode_advisory_for_healthy_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """README: "doctor also flags a stale installed bridge DLL and disabled
    DeepMode". The DeepMode advisory rides on an ok=True bridge check, so it
    must render even though the check itself passed."""
    from apm_suite import doctor

    monkeypatch.setattr(
        doctor,
        "_bridge_status",
        lambda: {
            "ok": True,
            "fix": "DeepMode off: per-entity AI/path sections will not be measured",
        },
    )
    result = runner.invoke(app, ["doctor", "--json", "-"], env={"COLUMNS": "4096"})
    assert result.exit_code == 0
    assert "DeepMode off" in result.output


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


def test_rank_folded_skips_unicode_digit_counts(tmp_path: Path) -> None:
    from apm_suite.analysis.report import _rank_folded

    # "²".isdigit() is True but int("²") raises: one folded line carrying a
    # Digit-class count (imported bundles) must skip that row, not fail the
    # required summary stage. Well-formed rows on either side still rank.
    folded = tmp_path / "stacks.folded"
    folded.write_text(
        "GameManager.Update;A.b \u00b2\nGameManager.Update;World.TickEntities 30\n",
        encoding="utf-8",
    )
    ranked = _rank_folded(folded, limit=10)
    assert dict(ranked["inclusive"]) == {"GameManager.Update": 100.0, "World.TickEntities": 100.0}


def test_export_scrub_redacts_nested_cmdline_exe() -> None:
    from apm_suite.bundle import _scrub

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
    # An operator-attached slice of the same server log is PII by content, not
    # by suffix: it must be excluded the same way, not merely home-scrubbed.
    (session / "app/efficientserver_log_excerpt.txt").write_text(
        "2026-08-23T10:00:00 42.0 INF Player 'Alice' joined from 203.0.113.7\n"
        "World.TickEntities=41.2ms(x100,max=99.0)\n"
    )

    bundle = tmp_path / "bundle.zip"
    result = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        events_line = archive.read("events.jsonl").decode()
        vfs = archive.read("io/vfs.bt.out").decode()
        svg = archive.read("cpu/perf/flame.svg").decode()
        # Scan the CONTENT of every member, not the members whose name looks
        # private: exclusion is the assertion about names, and the scrub is the
        # assertion about what survives inside the members that were kept.
        kept = {
            name: archive.read(name).decode("utf-8", errors="replace") for name in sorted(names)
        }
    assert home not in events_line + vfs + svg
    assert '"cmdline": "<redacted>"' in events_line
    assert "~/save" in events_line and "truncated-line ~/more" in events_line
    assert f"openat {home}" not in vfs and "openat ~/steamapps/common" in vfs
    assert "~/libgame.so" in svg
    assert "app/bridge.jsonl" not in names
    assert "app/efficientserver_log_excerpt.txt" not in names
    for name, text in kept.items():
        assert "203.0.113.7" not in text, f"{name} carried the scraped address"
        assert "Alice" not in text, f"{name} carried the player name"
        assert "203.0.113.7" not in name, f"{name} carried the scraped address in its name"


def test_export_survives_unparseable_meta_timestamp(tmp_path: Path) -> None:
    # meta.json is untrusted (hand-edited, imported bundle): a utc the session
    # cannot spell must not abort the export with a bare ValueError traceback
    # after the evidence has already been written into the bundle.
    import zipfile

    session = tmp_path / "session_bad_utc"
    session.mkdir()
    atomic_json(session / "meta.json", {"pid": 42, "only": "all", "utc": "not-a-timestamp"})
    bundle = tmp_path / "bundle_bad_utc.zip"
    result = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json").decode())
    assert manifest["target"]["pid"] == 42


def test_export_unreadable_member_names_file_and_keeps_prior_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact that cannot be read mid-export (raced prune, perms) must
    fail with the offending member named - like the .json branch of the same
    walk already does - instead of a bare traceback. The temp+replace build
    must also leave a pre-existing bundle at the target untouched."""
    import zipfile as zipfile_module

    session = tmp_path / "session_export_fail"
    (session / "io").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta())
    (session / "io/vfs.bt.out").write_text("openat /steamapps/common\n")

    def denied(self: Any, filename: str, arcname: str) -> None:
        raise PermissionError(13, "Permission denied", str(filename))

    # Force the open-failure fallback path: the stream scrubber reports it
    # could not read the source (monkeypatched to False, its documented
    # "source could not be opened" signal), so export falls back to a raw
    # copy - which then fails with the same OS error and must be named.
    monkeypatch.setattr("apm_suite.bundle._stream_scrubbed_member", lambda *a, **k: False)
    monkeypatch.setattr(zipfile_module.ZipFile, "write", denied)

    prior = tmp_path / "bundle.zip"
    prior.write_bytes(b"prior bundle bytes")
    result = runner.invoke(app, ["export", str(session), "--output", str(prior)])

    assert result.exit_code == 2
    assert "cannot bundle io/vfs.bt.out" in result.output
    assert prior.read_bytes() == b"prior bundle bytes"
    # No half-written temp zips stranded beside the target.
    leftovers = [p for p in tmp_path.glob("*.zip") if p.name != "bundle.zip"]
    assert leftovers == []


def test_export_streamed_text_members_match_full_text_scrub_bytes(tmp_path: Path) -> None:
    """The streaming scrubber must emit byte-identical output to the former
    full-text path: universal newlines normalized to LF, every kept line
    terminated with "\\n" (including a final line that had none), per-line
    JSONL redaction, and the home prefix replaced everywhere."""
    import zipfile

    home = str(Path.home())
    session = tmp_path / "session_stream"
    (session / "io").mkdir(parents=True)
    (session / "app").mkdir()
    atomic_json(session / "meta.json", _meta())
    # CRLF, a bare CR, no trailing newline: the text layer normalizes all of
    # them to LF before the line-wise scrub runs.
    vfs_raw = f"openat 1\nopenat {home}/steamapps\r\nclose\rno-newline-tail"
    (session / "io/vfs.bt.out").write_bytes(vfs_raw.encode("utf-8"))
    (session / "events.jsonl").write_text(
        json.dumps({"t": 1.0, "cmdline": "-quiet", "note": f"at {home}/x"})
        + "\n"
        + f"malformed trailing {home}/y"  # no newline at EOF
    )

    bundle = tmp_path / "bundle.zip"
    result = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert result.exit_code == 0, result.output

    # Expected bytes spelled out from the former implementation's contract,
    # NOT by calling the production scrubber: a comparison against the code
    # under test holds for any behaviour of that code, including doing nothing.
    expected = {
        "io/vfs.bt.out": "openat 1\nopenat ~/steamapps\nclose\nno-newline-tail\n",
        "events.jsonl": (
            '{"t": 1.0, "cmdline": "<redacted>", "note": "at ~/x"}\nmalformed trailing ~/y\n'
        ),
    }
    with zipfile.ZipFile(bundle) as archive:
        for name, want in expected.items():
            got = archive.read(name).decode("utf-8")
            assert got == want, name
            assert home not in got


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
    note = "player ☃ joined -> NFD: café"
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


def test_import_bundle_restores_owner_only_perms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/APM.md promises owner-only sessions; a restored bundle carries raw
    evidence too, so the claim must hold for imports, not just captures."""
    import zipfile

    bundle = tmp_path / "session_evidence.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("meta.json", "{}\n")

    store = tmp_path / "store"
    store.mkdir(mode=0o755)
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(store))
    result = runner.invoke(app, ["import", str(bundle)])
    assert result.exit_code == 0, result.output

    restored = store / "session_evidence"
    assert stat.S_IMODE(restored.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.stat().st_mode) == 0o700


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
    from apm_suite import bundle as bundle_module

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
    monkeypatch.setattr(bundle_module, "MAX_IMPORT_UNCOMPRESSED_BYTES", 4)
    result = runner.invoke(app, ["import", str(bundle)])
    assert result.exit_code == 2
    assert "import limits" in result.output
    assert not list(store.glob("session_*"))

    monkeypatch.setattr(bundle_module, "MAX_IMPORT_UNCOMPRESSED_BYTES", 2**40)
    monkeypatch.setattr(bundle_module, "MAX_IMPORT_MEMBERS", 1)
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


def test_object_list_keeps_only_object_records() -> None:
    assert object_list([{"a": 1}, "text", 5, None, ["nested"]]) == [{"a": 1}]
    # A scalar or object where a record list belongs reads as absent evidence.
    assert object_list({"a": 1}) == []
    assert object_list("layers") == []
    assert object_list(None) == []


def test_prometheus_tolerates_non_list_record_blocks(tmp_path: Path) -> None:
    """A record block that is not a list (or holds non-objects) reads as absent
    evidence; it must not raise out of the exporter."""
    session = _session(tmp_path / "session_shape")
    summary = load_json(session / "summary.json")
    summary["layers"] = {"cpu": 10}
    summary["metadata"] = {"lag_diagnosis": {"laggy": False, "causes": "gc_pauses"}}
    atomic_json(session / "summary.json", summary)
    atomic_json(
        session / "csharp_bridge.json",
        {"attribution": {"subsystems": {"World.TickEntities": {"scaled_total_ms": 9}}}},
    )
    out = tmp_path / "metrics.txt"
    result = runner.invoke(app, ["prometheus", str(session), "--output", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "layer_pressure{" not in text
    assert "subsystem_ms{" not in text
    assert "lag_cause_severity" not in text


def test_readers_tolerate_non_object_records_in_bridge_output(tmp_path: Path) -> None:
    """The same untrusted-shape contract on the other record readers: a
    csharp_bridge.json whose section list is a dict, or holds bare strings,
    reads as no sections instead of raising AttributeError mid-analysis."""
    session = _session(tmp_path / "session_bridge_shape")
    atomic_json(
        session / "csharp_bridge.json",
        {"top_managed_sections": ["World.TickEntities", {"name": "AI", "avgMs": 2}]},
    )
    assert ranked_section_heats(session) == {"AI": 2.0}
    atomic_json(session / "csharp_bridge.json", {"top_managed_sections": {"AI": {}}})
    assert ranked_section_heats(session) == {}
    assert analyze_scaling([session])["sections"] == []


def test_scaling_and_compare_tolerate_malformed_record_blocks(tmp_path: Path) -> None:
    """Same contract on the summary/bridge record lists the ladder fit and the
    before/after delta walk read."""
    session = _session(tmp_path / "session_ladder_shape")
    summary = load_json(session / "summary.json")
    summary["metadata"] = {"world": {"entities": 4, "players": 2}}
    summary["layers"] = ["cpu"]
    atomic_json(session / "summary.json", summary)
    result = analyze_scaling([session])
    assert result["scales"] == [2.0]
    assert result["sections"] == []
    assert layer_state(summary) == (set(), {})


def test_prometheus_label_escapes_every_line_terminator() -> None:
    """A raw CR inside a label value survives the scrape reader (only a trailing
    CR is stripped) and lands in the parsed value, so the terminators have to be
    escaped like the quote and the backslash, not just the newline."""
    from apm_suite.prometheus import _prom_label

    assert _prom_label("a\r\nb") == "a\\r\\nb"
    assert _prom_label('q"\\x') == 'q\\"\\\\x'
    # Non-ASCII is not escaped: the exposition is UTF-8 and the spec carries it.
    assert _prom_label("gr\u00f6\u00dfe") == "gr\u00f6\u00dfe"


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
    # A junk section duration stays present-at-0, so it is a tie and never a
    # bogus winner. A junk attribution total has no placeable duration at all,
    # so that subsystem is left out of the pairing entirely.
    sections = {d["section"]: d for d in cmp_doc["section_deltas"]}
    assert set(sections) == {"World.TickEntities"}, cmp_doc["section_deltas"]
    assert sections["World.TickEntities"]["a_heat"] == 0.0
    assert sections["World.TickEntities"]["b_heat"] == 0.0
    assert sections["World.TickEntities"]["better"] == "tie"
    assert [d["subsystem"] for d in cmp_doc["attribution_deltas"]] == []


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
    """The secret reaches the CLI only through the environment: an env-supplied
    password never echoes into output, and there is no --telnet-password flag
    that could leak it into shell history or /proc/<pid>/cmdline."""
    result = runner.invoke(
        app, ["capture", "--dry-run"], env={"SEVENDTD_TELNET_PASSWORD": "super-secret"}
    )
    assert result.exit_code == 0
    assert "super-secret" not in result.stdout

    rejected = runner.invoke(app, ["capture", "--telnet-password", "super-secret"])
    assert rejected.exit_code != 0
    assert "super-secret" not in rejected.output


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


def test_audit_rejects_manifest_paths_escaping_the_session(tmp_path: Path) -> None:
    """A planted manifest.json (imported bundles carry one) must not aim the
    recorded-hash check at files outside the session via absolute paths or
    '..' segments: those records are integrity failures, never reads."""
    session = _session(tmp_path / "session_escape")
    assert audit_session(session)[1]
    outside = tmp_path / "outside.secret"
    outside.write_text("host file the manifest must not reach")
    atomic_json(
        session / "manifest.json",
        {
            "schema": "7dtd.apm.manifest.v2",
            "session_id": session.name,
            "started_at": "2026-08-24T00:00:00+00:00",
            "target": {"pid": 1, "comm": "7DaysToDieServe", "exe": "", "cmdline": ""},
            "requested_layers": [],
            "artifacts": [
                # Absolute path and traversal both resolve outside the session;
                # sha256 is a syntactically valid digest so only the containment
                # check can reject these records.
                {"path": str(outside), "bytes": outside.stat().st_size, "sha256": "0" * 64},
                {"path": "../outside.secret", "bytes": 40, "sha256": "a" * 64},
            ],
        },
    )
    result = runner.invoke(app, ["audit", str(session)])
    assert result.exit_code == 1
    assert "escapes the session directory" in result.stderr
    assert "outside.secret" in result.stderr


def test_member_is_safe_rejects_lone_surrogate_paths() -> None:
    """A lone surrogate cannot be encoded to UTF-8, so the path can never name
    a real file on this host, yet joining it would crash os.stat with an
    uncaught UnicodeEncodeError instead of a diagnosable integrity error."""
    from apm_suite.io import member_is_safe

    assert not member_is_safe("sub_\ud800x/artifact.json")
    assert member_is_safe("sub_\ufffdx/artifact.json")  # scrubbed spelling is fine


def test_member_is_safe_rejects_control_and_bidi_member_names() -> None:
    """A newline in an archive member name is legal on Linux and produces a file
    whose name every line-oriented reader downstream splits in two; the bidi
    overrides and zero-width characters render as nothing, so a name carrying
    them names a different file than the one an operator reads. Both are planted
    by whoever supplies the bundle, so the shared guard is where they are cut."""
    from apm_suite.io import member_is_safe

    assert not member_is_safe("cpu/perf\nflame.json")
    assert not member_is_safe("cpu/perf\rmap.txt")
    assert not member_is_safe("cpu/\x00flame.json")
    assert not member_is_safe("cpu/perf\u202egnp.txt")  # RLO: renders as "gnp.txt"
    assert not member_is_safe("cpu/\u2066isolate.json")
    assert not member_is_safe("cpu/\ufeffflame.json")
    assert not member_is_safe("cpu/\x7f.json")
    # An ordinary non-ASCII name is evidence, not an attack: it must import.
    assert member_is_safe("logs/messungen-größe.json")
    assert member_is_safe("cpu/perf/flame.json")


def test_load_json_scrubs_lone_surrogate_escapes(tmp_path: Path) -> None:
    """JSON "\\ud800" escapes decode to lone surrogates that no atomic_* writer
    or filesystem call downstream can encode; the untrusted-reader boundary
    replaces them with U+FFFD so imported documents stay usable."""
    doc_path = tmp_path / "planted.json"
    # Written as literal backslash-u text: exactly what a crafted bundle ships.
    doc_path.write_text('{"p": "sub_\\ud800x", "n": {"\\udfff": [1]}}', encoding="utf-8")
    loaded = load_json(doc_path)
    assert loaded["p"] == "sub_\ufffdx"
    assert list(loaded["n"]) == ["\ufffd"]
    # The actual property callers rely on: the value survives a rewrite.
    atomic_json(tmp_path / "out.json", loaded)
    assert load_json(tmp_path / "out.json") == loaded


def test_loads_scrubbed_catches_every_escape_spelling() -> None:
    """The pre-scan that skips the recursive scrub must match every spelling a
    JSON document can carry a surrogate in: either case of the u, either hex
    digit of the D800-DFFF lead byte, in keys, values, and nested containers.
    A survivor here is what crashes a later atomic_* writer."""
    from apm_suite.io import loads_scrubbed

    for escape in ("\\ud800", "\\uD800", "\\uD83d", "\\udE00", "\\udfff"):
        value = loads_scrubbed('{"k": ["x' + escape + 'y"], "' + escape + '": 1}')
        assert value["k"] == ["x\ufffdy"], escape
        assert list(value) == ["k", "\ufffd"], escape
    # The common case stays untouched: a clean document round-trips as parsed.
    assert loads_scrubbed('{"a": [1, "b"]}') == {"a": [1, "b"]}


def test_iter_jsonl_scrubs_lone_surrogates(tmp_path: Path) -> None:
    """Planted JSONL records (an imported bundle's app/bridge.jsonl) get the
    same surrogate scrub as structured docs; their strings feed event messages
    and summary documents that are persisted with ensure_ascii=False."""
    from apm_suite.io import iter_jsonl

    lines = tmp_path / "planted.jsonl"
    lines.write_text('{"t": 1.0, "text": "a\\ud800b"}\n{"t": 2.0, "ok": true}\n', encoding="utf-8")
    records = list(iter_jsonl(lines))
    assert records[0]["text"] == "a\ufffdb"
    assert json.dumps(records[0], ensure_ascii=False).encode("utf-8")


def test_audit_survives_manifest_with_lone_surrogate_paths(tmp_path: Path) -> None:
    """A planted manifest.json whose artifact paths carry lone-surrogate escapes
    must degrade to per-artifact integrity errors like any other missing file,
    not crash the audit with UnicodeEncodeError out of the path join."""
    session = _session(tmp_path / "session_surrogate")
    assert audit_session(session)[1]
    planted = {
        "schema": "7dtd.apm.manifest.v2",
        "session_id": session.name,
        "started_at": "2026-08-24T00:00:00+00:00",
        "target": {"pid": 1, "comm": "7DaysToDieServe", "exe": "", "cmdline": ""},
        "requested_layers": [],
        "artifacts": [
            {"path": "sub_\ud800x/a.map", "bytes": 4, "sha256": "a" * 64},
            {"path": "b.map", "bytes": 4, "sha256": "b" * 64},
        ],
    }
    # Default dumps() escapes the surrogates: the on-disk bytes a hostile
    # bundle would carry, ASCII-safe to write here.
    (session / "manifest.json").write_text(json.dumps(planted), encoding="utf-8")
    result = runner.invoke(app, ["audit", str(session)])
    # SystemExit(1) is the CLI's INVALID verdict; any other exception would be
    # the pre-fix UnicodeEncodeError escaping the audit.
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "is recorded but missing" in result.stderr
    assert "UnicodeEncodeError" not in (result.stdout + result.stderr)


def test_audit_error_output_renders_markup_as_text(tmp_path: Path) -> None:
    """Audit errors quote untrusted strings (imported-bundle artifact paths,
    schema input values). A name carrying rich markup must print literally:
    if the console interpreted it, the tag would vanish into styling instead
    of reaching the operator (terminal markup injection)."""
    session = _session(tmp_path / "session_markup")
    assert audit_session(session)[1]
    atomic_json(
        session / "manifest.json",
        {
            "schema": "7dtd.apm.manifest.v2",
            "session_id": session.name,
            "started_at": "2026-08-24T00:00:00+00:00",
            "target": {"pid": 1, "comm": "7DaysToDieServe", "exe": "", "cmdline": ""},
            "requested_layers": [],
            "artifacts": [
                {"path": "[green]pwned[/green].map", "bytes": 4, "sha256": "a" * 64},
            ],
        },
    )
    result = runner.invoke(app, ["audit", str(session)])
    assert result.exit_code == 1
    # The brackets survive rendering verbatim: proof they were escaped, not
    # consumed as console markup (an interpreted tag would disappear).
    assert "[green]pwned[/green]" in result.stderr


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
    # Valid non-object JSON and a hand-edited bool both used to reach
    # float()/AttributeError instead of the documented default; the bridge
    # itself rejects either, so the monitor must too.
    (config / "apmbridge.json").write_text("[1, 2]")
    assert bridge_export_period(telemetry) == 30.0
    atomic_json(config / "apmbridge.json", {"PeriodicExportSeconds": True})
    assert bridge_export_period(telemetry) == 30.0
    # Above the bridge's own clamp: the mod would export hourly at most, so
    # the stale-read threshold must not wait on a read that never comes.
    atomic_json(config / "apmbridge.json", {"PeriodicExportSeconds": 99999})
    assert bridge_export_period(telemetry) == 3600.0


def test_bridge_config_readers_accept_the_commented_example(tmp_path: Path) -> None:
    """The example config install_bridge.sh seeds carries // and /* */ comments
    (Json.NET skips them on read), so every Python reader of the live config
    must parse the same dialect instead of falling back to defaults."""
    from apm_suite.cli import bridge_export_period

    telemetry = tmp_path / "telemetry"
    telemetry.mkdir()
    config = tmp_path / "Config"
    config.mkdir()
    (config / "apmbridge.json").write_text(
        '{\n  // cadence\n  "PeriodicExportSeconds": 45,\n'
        '  /* block\n     comment */\n  "DeepMode": true\n}\n'
    )
    assert bridge_export_period(telemetry) == 45.0

    # A commented install must also reach doctor's DeepMode advisory.
    from apm_suite import doctor

    mods = tmp_path / "Mods/7dtd-server-apm-bridge"
    (mods / "Config").mkdir(parents=True)
    (mods / "7dtd-server-apm-bridge.dll").write_bytes(b"dll")
    shutil.copy(config / "apmbridge.json", mods / "Config/apmbridge.json")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(doctor, "bridge_mod_dir", lambda: mods)
    monkey.setattr(doctor, "REPO", tmp_path)
    try:
        assert doctor._bridge_status()["deep_mode"] is True
    finally:
        monkey.undo()


def test_strip_json_comments_keeps_strings_and_line_numbers(tmp_path: Path) -> None:
    """A regex strip would eat "http://" inside a value; a stripped comment
    must still occupy its lines so a parse error points at the operator's."""
    from apm_suite.io import strip_json_comments

    assert json.loads(strip_json_comments('{"u": "http://h/a//b"}'))["u"] == "http://h/a//b"
    assert json.loads(strip_json_comments('{"q": "a /* b */ c", "n": 1}')) == {
        "q": "a /* b */ c",
        "n": 1,
    }
    text = '{\n  // one\n  /* two\n     three */\n  "n": ?\n}\n'
    stripped = strip_json_comments(text)
    assert stripped.count("\n") == text.count("\n")
    bad = tmp_path / "apmbridge.json"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="cannot parse"):
        load_jsonc(bad)


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


def test_audit_sees_perf_result_nested_two_dirs_deep(tmp_path: Path) -> None:
    # The perf collector's result lands at cpu/perf/perf.result.json; a glob
    # matching only one directory level would drop perf from the manifest and
    # never warn about its failures.
    session = _session(tmp_path / "session_perf_nested")
    (session / "cpu/perf").mkdir(parents=True)
    atomic_json(
        session / "cpu/perf/perf.result.json", _result_json("failed", name="perf", layer="cpu")
    )
    manifest, valid = audit_session(session)
    assert valid
    perf = next(c for c in manifest.collectors if c.name == "perf")
    assert perf.status == "failed"
    assert any("perf" in w for w in manifest.warnings)


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
    assert not valid, "unparseable meta.json must record the session as invalid"
    assert manifest.target.pid == 1
    assert manifest.requested_layers == ["all"]


def test_audit_survives_torn_meta_json(tmp_path: Path) -> None:
    # Torn/hand-edited meta.json (invalid JSON) degrades to no-metadata plus a
    # recorded schema-validation error; it must not traceback out of the audit.
    session = _session(tmp_path / "session_torn_meta")
    (session / "meta.json").write_text("{oops\n", encoding="utf-8")
    manifest, valid = audit_session(session)
    assert not valid
    assert any("meta.json" in e for e in manifest.errors)


def test_audit_reports_unreadable_documents_instead_of_crashing(tmp_path: Path) -> None:
    # A document that vanished or cannot be read (concurrent prune, perms) is an
    # audit finding to record, never a traceback out of the audit whose job is to
    # report exactly that.
    session = _session(tmp_path / "session_unreadable")
    denied = {"meta.json", "summary.json", "health.json"}
    original = Path.open

    def deny(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name in denied:
            raise PermissionError(13, "Permission denied")
        return original(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", deny)
        manifest, valid = audit_session(session)
    assert not valid
    assert any("summary.json" in e and "could not read" in e for e in manifest.errors)
    assert any("meta.json" in e and "could not read" in e for e in manifest.errors)


def test_audit_reports_unreadable_collector_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Denying the read through the open() the audit actually uses keeps the
    # branch covered everywhere; a chmod(0o000) fixture silently skips on any
    # host that can read it anyway (root), leaving the finding unverified.
    session = _session(tmp_path / "session_unreadable_result")
    (session / "sync").mkdir()
    atomic_json(session / "sync/futex.result.json", _result_json("ok", name="futex", layer="sync"))
    original = Path.open

    def deny(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.endswith(".result.json"):
            raise PermissionError(13, "Permission denied")
        return original(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", deny)
        manifest, valid = audit_session(session)
    assert not valid
    assert any("unreadable collector result" in e for e in manifest.errors)


def test_app_scrape_with_only_failed_records_is_not_evidence(tmp_path: Path) -> None:
    # app_scrape logs every attempt, so a full artifact can hold nothing but
    # failures (telnet down, password rejected). The app_sim layer must read as
    # unavailable then, not as collected with an empty payload.
    from apm_suite.analysis.report import layer_scores

    session = _session(tmp_path / "session_scrape_failed")
    (session / "app").mkdir()
    (session / "app/bridge.jsonl").write_text(
        '{"t": 1.0, "ok": false, "error": "connection refused"}\n'
        '{"t": 2.0, "ok": false, "error": "connection refused"}\n',
        encoding="utf-8",
    )
    app = next(s for s in layer_scores(session, {}, {}) if s.layer == "app_sim")
    assert app.state == "unavailable"
    assert "detail" in app.signals


def test_app_scrape_with_one_success_stays_collected(tmp_path: Path) -> None:
    from apm_suite.analysis.report import layer_scores

    session = _session(tmp_path / "session_scrape_ok")
    (session / "app").mkdir()
    (session / "app/bridge.jsonl").write_text(
        '{"t": 1.0, "ok": false, "error": "connection refused"}\n'
        '{"t": 2.0, "ok": true, "text": "apm status\\nTPS 20\\n"}\n',
        encoding="utf-8",
    )
    app = next(s for s in layer_scores(session, {}, {}) if s.layer == "app_sim")
    assert app.state == "collected"


# --- unit: store restore verification -----------------------------------------


def test_verify_store_reports_copied_back_store_without_writing(tmp_path: Path) -> None:
    """A restored copy is only proven by a check that cannot heal itself: the
    verdict must come from the recorded hashes, and no manifest may be
    rewritten while verifying (a re-stamp would absorb the drift being looked
    for)."""
    root = tmp_path / "restored"
    root.mkdir()
    session = _session(root / "session_copy")
    audit_session(session)
    baseline = load_json(session / "manifest.json")
    (session / "summary.json").write_text('{"tampered": true}\n')

    result = runner.invoke(app, ["verify-store", str(root)])
    assert result.exit_code == 1
    assert "INVALID" in result.stdout
    assert "summary.json" in result.stderr
    # Read-only: the recorded baseline the failed check compared against stays.
    assert load_json(session / "manifest.json") == baseline


def test_verify_store_passes_intact_copy_and_flags_incomplete_sessions(tmp_path: Path) -> None:
    root = tmp_path / "restored"
    root.mkdir()
    intact = _session(root / "session_intact")
    audit_session(intact)
    # A capture still running, or one copied before finalize: required documents
    # missing, but nothing contradicts a recorded hash.
    partial = root / "session_partial"
    partial.mkdir()
    atomic_json(partial / "meta.json", _meta())

    result = runner.invoke(app, ["verify-store", str(root)])
    assert result.exit_code == 0
    # Rich wraps the summary line at the console width; compare unwrapped.
    assert "1 ok, 1 incomplete, 0 invalid" in " ".join(result.stdout.split())
    assert "incomplete  session_partial" in result.stdout
    assert "missing or empty: report.html" in result.stderr

    # --strict turns "copy what is there" into a failed drill.
    strict = runner.invoke(app, ["verify-store", str(root), "--strict"])
    assert strict.exit_code == 1


def test_verify_store_reports_a_session_never_audited(tmp_path: Path) -> None:
    """A session with no manifest.json has no integrity baseline: it must not
    count as verified, or a copy that silently lost the manifests still looks
    green."""
    root = tmp_path / "restored"
    root.mkdir()
    session = _session(root / "session_unbaselined")

    result = runner.invoke(app, ["verify-store", str(root)])
    assert result.exit_code == 0
    assert "incomplete" in result.stdout
    assert "no manifest.json recorded" in result.stderr
    assert runner.invoke(app, ["verify-store", str(root), "--strict"]).exit_code == 1
    # The command stays read-only on a healthy store too.
    assert not (session / "manifest.json").exists()


def test_verify_store_rejects_a_path_that_is_not_a_store(tmp_path: Path) -> None:
    missing = tmp_path / "not-a-store"
    result = runner.invoke(app, ["verify-store", str(missing)])
    assert result.exit_code == 2
    assert "not a store directory" in result.stderr


# --- parser fixtures ----------------------------------------------------------


def test_parse_perf_stat_fixture() -> None:
    parsed = parse_perf_stat((FIXTURES / "hw_stat.txt").read_text())
    assert parsed["cycles"] == 123456789
    assert parsed["instructions"] == 98765432
    assert parsed["time_elapsed_s"] == pytest.approx(10.01)


def test_parse_perf_stat_drops_non_finite_counters() -> None:
    # hw_stat.txt is re-read without schema guarantees (imported bundles, hand
    # edits): a digit run past the double range parses to inf, and inf/inf
    # would poison ipc with a NaN that persists into summary.json as a bare
    # NaN strict JSON consumers reject. A non-finite counter is absent evidence.
    parsed = parse_perf_stat(
        "123 cycles\n"
        + "9" * 400
        + " instructions\n"
        + "9" * 400
        + " seconds time elapsed\n"
        + "garbage-line without numbers\n"
    )
    assert parsed == {"cycles": 123}


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


def test_events_bound_keeps_the_worst_events_not_the_first(tmp_path: Path) -> None:
    """Past the retention bound the timeline keeps errors over warnings over
    info (recency breaking the tie), not whichever events parsed first: a long
    busy window must not drop its ending stall behind thousands of quiet
    opening samples."""
    session = tmp_path / "session_bound"
    (session / "sync").mkdir(parents=True)
    # PER_SOURCE_MAX is 500 per source, so several sources are needed to pass
    # RETAINED_MAX: 2400 futex warnings plus one error from the last source.
    (session / "sync/futex.bt.out").write_text(
        "\n".join(f"SLOW_FUTEX tid=1 wait={i}ms" for i in range(PER_SOURCE_MAX))
    )
    (session / "io").mkdir(parents=True)
    (session / "io/vfs.bt.out").write_text(
        "\n".join(f"SLOW_VFS_MAIN tid=1 wait={i}ms" for i in range(PER_SOURCE_MAX))
    )
    (session / "io/block.bt.out").write_text(
        "\n".join(f"SLOW_BLOCK tid=1 wait={i}ms" for i in range(PER_SOURCE_MAX))
    )
    (session / "runtime").mkdir(parents=True)
    (session / "runtime/mono_gc.bt.out").write_text(
        "\n".join(f"SLOW mono_gc_collect tid=1 wait={i}ms" for i in range(PER_SOURCE_MAX))
    )
    # The bridge spike is the only error, and it lands last in parse order.
    (session / "app").mkdir(parents=True)
    atomic_json(
        session / "app/apm_app.json",
        {
            "spikes": [
                {"utc": f"2026-01-01T00:00:{i:02d}Z", "gmUpdateDurationMs": 900 + i}
                for i in range(PER_SOURCE_MAX)
            ]
        },
    )
    doc = build_timeline(session)
    assert doc.count > 2000
    assert len(doc.events) == 2000
    assert doc.dropped == doc.count - 2000
    assert [e.severity for e in doc.events].count("info") == 0
    # Every surviving error is a frame spike; the whole error tier survives,
    # so none of the recorded spike durations is lost to the bound.
    errors = [e for e in doc.events if e.severity == "error"]
    assert len(errors) == PER_SOURCE_MAX
    assert all(e.kind == "frame_spike" for e in errors)
    assert sorted(e.value for e in errors if e.value is not None) == [
        900 + i for i in range(PER_SOURCE_MAX)
    ]
    # The materialized set is still laid out chronologically.
    stamps = [e.t for e in doc.events if e.t is not None]
    assert stamps == sorted(stamps)


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


def _stackcollapse_run(argument: str, stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    # LANG=C plus -X utf8=0 forces the pre-3.15 default: stdio follows the
    # locale, so an ASCII stdin/stdout is what this collector really sees when
    # it runs under a bare systemd unit or cron.
    env = {**os.environ, "LANG": "C", "LC_ALL": "C", "PYTHONUTF8": "0"}
    return subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8=0",
            str(REPO / "tools/host_profiler/stackcollapse_perf.py"),
            argument,
        ],
        input=stdin,
        capture_output=True,
        env=env,
        check=False,
    )


def test_stackcollapse_io_is_utf8_under_a_c_locale(tmp_path: Path) -> None:
    """Non-ASCII frame names must survive both ends, from either input path.

    Under LANG=C the default stdio encoding is ASCII: printing a folded key
    holding a non-ASCII character raised UnicodeEncodeError and lost the whole
    stacks.folded, and reading the same bytes from stdin (sys.stdin's locale
    encoding plus surrogateescape) took a different invalid-byte policy than
    the file path. Both now pin UTF-8 with U+FFFD replacement.
    """
    non_ascii = b"srv 1/1 [000] 1.0: cycles:\n\t7f01 caf\xc3\xa9+0x42 (/tmp/perf-1.map)\n\n"
    script = tmp_path / "perf.script"
    script.write_bytes(non_ascii)

    from_path = _stackcollapse_run(str(script), b"")
    piped = _stackcollapse_run("-", non_ascii)
    for label, result in (("file", from_path), ("stdin", piped)):
        assert result.returncode == 0, f"{label}: {result.stderr!r}"
        assert result.stdout == b"caf\xc3\xa9 1\n", f"{label}: {result.stdout!r}"

    undecodable = b"srv 1/1 [000] 1.0: cycles:\n\t7f01 ab\xff\xfe+0x42 (/t)\n\n"
    invalid = _stackcollapse_run("-", undecodable)
    assert invalid.returncode == 0
    # U+FFFD, and no lone surrogate: the consumer re-reads this file as UTF-8
    # and would crash on a surrogate that only survived via surrogateescape.
    assert invalid.stdout.decode("utf-8") == "ab�� 1\n"
    assert b"\xed" not in invalid.stdout


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


def test_attribution_survives_junk_section_fields_and_snapshot_scales() -> None:
    """One malformed section must cost that section, not the whole block.

    Imported bundles carry unvalidated sections, and the per-field coercion
    used here is what keeps a list "totalMs" or a non-numeric "calls" from
    raising out of attribute_snapshot's guard (which drops ALL attribution).
    A junk deepSampleRate must likewise not scale every deep section to 0 ms.
    """
    from apm_suite.analysis.bridge import attribute_subsystems

    sections: list[dict[str, Any]] = [
        {"name": "DecoManager.UpdateTick", "totalMs": [1], "calls": "many", "avgMs": "junk"},
        {"name": "World.TickEntities", "totalMs": 100.0, "calls": 10, "deep": True},
    ]
    result = attribute_subsystems(sections, deep_sample_rate=0, window_updates=100, entities=5)
    by_name = {s["subsystem"]: s for s in result["subsystems"]}
    # The junk section contributes nothing; the good one still scales by 1.
    assert by_name["entity_tick"]["scaled_total_ms"] == 100.0
    assert result["measured_ms"] == 100.0


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


def test_app_scrape_events_survive_mangled_and_huge_durations(tmp_path: Path) -> None:
    from apm_suite.analysis.events import build_events

    session = tmp_path / "session_scrape_bad"
    (session / "app").mkdir(parents=True)
    # bridge.jsonl interleaves server-controlled console text: a mangled
    # duration ("1.2.3ms" -> float ValueError) must not fail the required
    # events stage, and a digit run past the double range (float -> inf) must
    # drop its value instead of persisting a bare Infinity that strict JSON
    # consumers reject. A well-formed spike on either side still lands.
    records = [
        {"t": 1.0, "ok": True, "text": "SPIKE gmUpdateDuration=1.2.3ms"},
        {"t": 2.0, "ok": True, "text": "SPIKE gmUpdateDuration=" + "9" * 400 + "ms"},
        {"t": 3.0, "ok": True, "text": "SPIKE gmUpdateDuration=120.00ms"},
    ]
    (session / "app/bridge.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    doc = build_events(session)
    spikes = [e for e in doc.events if e.kind == "managed_bridge_spike"]
    assert len(spikes) == 3
    assert spikes[0].model_dump(mode="json").get("value") is None
    assert spikes[1].model_dump(mode="json").get("value") is None
    assert spikes[2].model_dump(mode="json")["value"] == pytest.approx(120.0)

    # Strict round-trip: no Infinity/NaN literal may reach the persisted files.
    def reject_constant(name: str) -> None:
        raise ValueError(f"non-finite JSON constant {name}")

    blob = (session / "events.json").read_text()
    assert "Infinity" not in blob and "NaN" not in blob
    json.loads(blob, parse_constant=reject_constant)
    for line in (session / "events.jsonl").read_text().splitlines():
        assert "Infinity" not in line and "NaN" not in line
        json.loads(line, parse_constant=reject_constant)


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
    # the identical deep-sample scaling as the session-level entry point:
    # 100ms sampled 1-in-16 reads as 1600ms, the always-sampled 400ms as-is.
    snapshot = attribute_snapshot(session)
    assert snapshot == attribute_document(doc)
    assert snapshot is not None
    assert snapshot["deep_sample_rate"] == 16
    drill = {d["level"]: d["scaled_total_ms"] for d in snapshot["entity_drilldown"]["levels"]}
    assert drill["tick_entity"] == 1600.0
    subsystems = {s["subsystem"]: s["scaled_total_ms"] for s in snapshot["subsystems"]}
    assert subsystems == {"deco_world": 400.0}
    assert snapshot["measured_ms"] == 400.0  # additive buckets only, not the chain


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
    assert "lag_diagnosis" in summary.metadata
    assert "cpu_hot_paths" in summary.metadata
    # The string fields must not have reached the frame arithmetic: the gap is
    # the difference of the coerced numbers, not absent evidence.
    assert summary.metadata["frame"]["engineGapMs"] == pytest.approx(13.4)


def test_build_summary_survives_snapshot_with_infinite_fields(tmp_path: Path) -> None:
    """JSON "1e999" parses to float('inf'): int() on it raises OverflowError
    (not ValueError) and round() leaves inf that json.dumps would persist as a
    bare `Infinity` strict JSON consumers reject. The summary stage must keep
    the host evidence and emit valid JSON instead."""
    from apm_suite.analysis.report import build_summary

    session = tmp_path / "session_infsnap"
    (session / "app").mkdir(parents=True)
    # Raw text on purpose: json.dumps never emits these literals.
    (session / "app/apm_app.json").write_text(
        '{"sections": [], "measurement": {"deepSampleRate": 1},'
        ' "update": {"windowUpdates": 100}, "world": {"entities": 10},'
        ' "gc": {"heapDeltaBytes": 1e999, "windowSeconds": 1e999}}',
    )
    atomic_json(session / "meta.json", _meta())
    summary = build_summary(session)  # must not raise
    path = session / "summary.json"
    text = path.read_text()
    assert "Infinity" not in text  # valid JSON for strict external consumers
    assert json.loads(text)["schema"] == "7dtd.apm.summary.v2"
    # The host-side blocks below the snapshot are built regardless of it.
    assert "lag_diagnosis" in summary.metadata
    # as_number rejects inf, so the block survives with the inf fields read as
    # absent evidence (0) rather than rates computed from Infinity/0.
    assert summary.metadata["gc"]["heapDeltaBytes"] == 0
    assert summary.metadata["gc"]["windowSeconds"] == 0.0
    assert summary.metadata["gc"]["allocMBPerSecond"] == 0


def test_build_summary_survives_snapshot_with_non_object_blocks(tmp_path: Path) -> None:
    """A snapshot block that parsed as a non-object ("gc": [16.6]) has no .get:
    _snapshot_metadata raises AttributeError from it, and like every other
    malformed-value escape that must drop only the snapshot-derived blocks -
    not fail the required summary stage and lose the host evidence."""
    from apm_suite.analysis.report import build_summary

    session = tmp_path / "session_listsnap"
    (session / "app").mkdir(parents=True)
    atomic_json(
        session / "app/apm_app.json",
        {
            "sections": [],
            "measurement": {"deepSampleRate": 1},
            "update": [100],
            "world": {"entities": 10},
            "gc": [16.6],
            "mapTransfers": ["junk"],
        },
    )
    atomic_json(session / "meta.json", _meta())
    summary = build_summary(session)  # must not raise
    assert "lag_diagnosis" in summary.metadata  # host evidence survived
    assert "gc" not in summary.metadata  # snapshot-derived blocks dropped whole


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


def test_snapshot_metadata_degrades_one_junk_field_not_the_block() -> None:
    """A string or container scalar in a hand-edited / imported snapshot must
    degrade that one field to absent evidence. Before, "16.6" - 3.2 raised
    TypeError and build_summary dropped the whole snapshot block; the sibling
    windowSeconds field is coerced for the same reason."""
    from apm_suite.analysis.report import _snapshot_metadata

    meta = _snapshot_metadata(
        {
            "world": {"unityDeltaMs": "16.6"},
            "update": {"gmUpdateDurationAvgMs": 3.2, "windowUpdates": "many"},
            "gc": {"windowSeconds": 30, "heapDeltaBytes": 1024},
            "mapTransfers": [{"name": "chunk", "bytes": 1048576, "packages": "lots"}],
        },
        "",
    )
    assert meta["frame"]["engineGapMs"] == 13.4
    assert meta["gc"]["windowSeconds"] == 30.0  # block survives the junk scalar
    assert meta["transfers"]["mb_per_second"] == 0.03
    assert meta["transfers"]["packages_per_second"] == 0.0


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


def test_section_rank_survives_junk_typed_section_fields() -> None:
    """Imported bundles sweep arbitrary app/*.json into the section table: a
    truthy non-numeric field ("avgMs": [5]) must degrade to a present-at-0 tie
    (compare.load_sections' posture), never raise TypeError/ValueError out of
    the standalone `bridge` ranking. Valid sections rank unchanged."""
    from apm_suite.analysis.bridge import section_rank

    ranked = section_rank(
        [
            {"name": "Junk.All", "avgMs": [5], "calls": {"x": 1}, "totalMs": "abc", "p95Ms": [1]},
            {"name": "Good.Section", "avgMs": 2.0, "calls": 10},
            {"name": "P95.Zero", "avgMs": 9.0, "calls": 3, "p95Ms": 0, "totalMs": 27.0},
        ]
    )
    scores = {s["name"]: s["score"] for s in ranked}
    assert scores["Good.Section"] == 2.0
    assert scores["Junk.All"] == 0.0
    # A legitimate p95Ms of 0 stays 0 instead of falling through to the legacy
    # `p95` field (ranked_section_heats' documented semantics); the score rule
    # then falls back to avgMs because an unmeasured p95 cannot rank.
    p95_zero = next(s for s in ranked if s["name"] == "P95.Zero")
    assert p95_zero["p95"] == 0.0
    assert scores["P95.Zero"] == 9.0


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
    # The annotated twin wins over the raw probe output it was derived from.
    (runtime / "mono_alloc.bt.out").write_text(
        "=== top sampled (1/4096, all sizes) (top 20) ===\n"
        "@alloc_bytes[\n"
        "        GC_malloc+0\n"
        "        Raw.Unannotated+0x1\n"
        "]: 12345678\n"
    )
    assert top_churn_sites(tmp_path) == ["EntityAlive.updateTasks"]
    text = _alloc_source_text(tmp_path)
    assert top_churn_sites(tmp_path, text=text) == top_churn_sites(tmp_path)
    assert "EntityAlive" in text and "Raw.Unannotated" not in text


def test_jitsym_annotates_hex_against_map(tmp_path: Path) -> None:
    from apm_suite.analysis.jitsym import annotate, load_map

    map_file = tmp_path / "perf-1.map"
    map_file.write_text("41e0155e 100 EntityAlive.updateTasks\n41c30f08 80 AstarManager.FindPath\n")
    starts, entries = load_map(map_file)
    out = annotate("site 0x41e0155e and 0x41c30f20 and 0xdeadbeef", starts, entries)
    assert "EntityAlive.updateTasks+0x0" in out
    assert "AstarManager.FindPath+0x18" in out
    assert "0xdeadbeef" in out  # outside any range: left as-is


def test_jitsym_annotate_session_streams_and_skips_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streaming rewrite must match the former whole-text pass: a changed
    probe gets a byte-identical .annotated.txt twin; a probe whose hex never
    resolves gets NO twin (an empty one would shadow the raw evidence for
    readers that prefer the annotated file)."""
    from apm_suite.analysis.jitsym import annotate, annotate_session, load_map

    (tmp_path / "cpu/perf").mkdir(parents=True)
    (tmp_path / "runtime").mkdir()
    map_file = tmp_path / "cpu/perf/perf-7.map"
    map_file.write_text("41e0155e 100 EntityAlive.updateTasks\n")
    monkeypatch.chdir(tmp_path)
    resolvable = tmp_path / "runtime" / "mono_alloc.bt.out"
    resolvable.write_text("site 0x41e0155e\nplain line\n")
    unresolvable = tmp_path / "runtime" / "futex.bt.out"
    unresolvable.write_text("addr 0xdeadbeff no match\n")
    plain = tmp_path / "scheduler" / "runqlat.bt.out"
    plain.parent.mkdir(parents=True)
    plain.write_text("no hex at all\n")

    assert annotate_session(tmp_path) == 1

    annotated = tmp_path / "runtime" / "mono_alloc.bt.annotated.txt"
    assert annotated.is_file()
    # Byte parity with the whole-text implementation, including line endings.
    assert annotated.read_text() == annotate(resolvable.read_text(), *load_map(map_file))
    # ...and the parity is not a shared no-op: the symbol really was resolved.
    assert "EntityAlive.updateTasks+0x0" in annotated.read_text()
    assert not (tmp_path / "runtime" / "futex.bt.annotated.txt").exists()
    assert not (tmp_path / "scheduler" / "runqlat.bt.annotated.txt").exists()


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
    "text,little,stw_total,stw_count,stw_worst",
    [
        ("", None, 0.0, 0, 0.0),  # probe produced nothing (attach failed / server down)
        ("garbage line\nno markers here\n", None, 0.0, 0, 0.0),
        # Truncated / malformed markers: the token is counted, the number is
        # never invented from it.
        ("@little_n:\nSTW_PAUSE us\n@stw_sum: notanumber\n", None, 0.0, 1, 0.0),
        ("STW_PAUSE 999", None, 0.0, 1, 0.0),  # missing unit suffix
        ("@little_n: 5" * 5000, 5, 0.0, 0, 0.0),  # pathological repetition
    ],
)
def test_gc_layer_survives_malformed_probe_output(
    text: str, little: int | None, stw_total: float, stw_count: int, stw_worst: float
) -> None:
    from apm_suite.analysis.report import _gc_layer

    layer = _gc_layer({"mono_gc": text})  # must not raise
    assert layer.layer == "runtime_gc"
    assert layer.signals == {
        "slow_gc_lines": 0,
        "collect_a_little_hits": little,
        "stw_pause_total_ms": stw_total,
        "stw_pause_count": stw_count,
        "stw_pause_worst_ms": stw_worst,
    }
    # Unparseable probe output is UNKNOWN, never pressure.
    assert layer.score == 0


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
    assert ok, "a skipped metric must not fail the budget on absent evidence"
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


@pytest.mark.parametrize(
    "token,expected",
    [
        (
            "all",
            {
                "app",
                "threads",
                "proc",
                "hw",
                "perf",
                "oncpu",
                "runqlat",
                "offcpu",
                "states",
                "futex",
                "vfs",
                "block",
                "io_net",
                "mono_gc",
            },
        ),
        ("app", {"app"}),
        ("app_sim", {"app"}),
        ("threads", {"threads", "proc"}),
        ("memory", {"proc", "hw"}),
        ("hw", {"proc", "hw"}),
        ("cache", {"proc", "hw"}),
        ("proc", {"proc", "hw"}),
        ("cpu", {"perf", "oncpu"}),
        ("sched", {"runqlat", "offcpu", "states"}),
        ("locks", {"futex"}),
        ("sync", {"futex"}),
        ("futex", {"futex"}),
        ("net", {"vfs", "block", "io_net"}),
        ("io", {"vfs", "block", "io_net"}),
        ("gc", {"mono_gc"}),
        ("runtime", {"mono_gc"}),
        ("alloc", {"mono_alloc"}),
        ("allocsites", {"mono_alloc"}),
        ("mono_alloc", {"mono_alloc"}),
        ("nonsense", set()),
    ],
)
def test_only_token_resolves_to_the_expected_collector_plan(token: str, expected: set[str]) -> None:
    """--only token -> planned collector set, pinned explicitly. Both the
    capture plan and the session audit resolve tokens, and a disagreement
    surfaces as false "requested collector produced no usable evidence"
    warnings (or silently missing ones) in every manifest, so both sides are
    held to the same expected set rather than to each other."""
    from apm_suite.capture import SPECS, wanted
    from apm_suite.session import _requested

    assert {spec.name for spec in SPECS if wanted(spec, token)} == expected, "capture plan"
    audited = {
        name
        for name, layer in ((s.name, s.layer) for s in SPECS)
        if _requested(name, layer, {token})
    }
    assert audited == expected, "session audit"


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


def test_planned_layers_derive_from_the_catalog_and_the_requested_tokens() -> None:
    """meta.json records the plan's real layers, not a hand-written list: a
    literal drifts from the catalog and cannot reflect --only, so every
    partial capture used to claim the same full-session layer set."""
    from apm_suite.capture import SPECS
    from apm_suite.collectors import planned_layers

    catalog_layers = sorted({spec.layer for spec in SPECS})
    assert planned_layers("all") == catalog_layers
    assert planned_layers("cpu,sched") == ["cpu", "scheduler"]
    assert planned_layers("net") == ["io"]
    # no-app drops the bridge-backed layer, not every layer that owns "app"
    # in its name.
    assert "app_sim" in planned_layers("all")
    assert "app_sim" not in planned_layers("all", no_app=True)
    assert planned_layers("app", no_app=True) == []
    # An opt-in collector counts once its own token is asked for.
    assert "runtime_gc" not in planned_layers("sched")
    assert "runtime_gc" in planned_layers("alloc")


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
    assert by_layer["sync_locks"].score == 35.0  # one 5ms+ wait over 10s
    assert by_layer["sync_locks"].signals["slow_futex_lines"] == 1
    assert by_layer["cpu"].state == "skipped"  # not requested -> no fake zero
    assert by_layer["cpu"].score is None


def test_build_summary_counts_offcpu_evidence_for_scheduler(tmp_path: Path) -> None:
    """--only offcpu produces scheduler evidence (stall/d_state/runq parsing);
    the layer must read collected, not have its only artifact ignored."""
    from apm_suite.analysis.report import build_summary

    session = tmp_path / "session_offcpu"
    (session / "scheduler").mkdir(parents=True)
    (session / "scheduler/offcpu.bt.out").write_text("@stall_us_total: 60000\n")
    atomic_json(session / "meta.json", _meta(only="offcpu"))
    summary = build_summary(session)
    scheduler = next(layer for layer in summary.layers if layer.layer == "scheduler")
    assert scheduler.state == "collected"
    assert scheduler.signals["main_thread_offcpu_ms"] == 60.0
    assert scheduler.score == 0.0  # pacing sleep is not lag on its own


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


def test_every_generated_page_carries_the_shared_tokens(tmp_path: Path) -> None:
    """Report, dashboard, session index, flame delta, and the interactive
    flamegraph are five views of one product. Each used to inline its own copy
    of the palette and they drifted (a card radius and a 14px body on the
    dashboard, the 16px UA default on the others, a hand-copied :root block in
    the flame page). The one source is apm_suite.web_tokens, so a page's
    stylesheet must declare the tokens and must not name a raw color of its
    own."""
    from apm_suite.analysis.index import html_index
    from apm_suite.web_tokens import TOKENS

    session = tmp_path / "session_tokens"
    session.mkdir()
    atomic_json(session / "summary.json", _summary("session_tokens", []))
    render_session(session)

    # host_profiler holds standalone scripts, not a package, so it is loaded by
    # path the same way the other script tests do.
    import importlib.util

    def _load(name: str) -> Any:
        spec = importlib.util.spec_from_file_location(name, REPO / f"tools/host_profiler/{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    flame = _load("flame_diff_html")
    interactive = _load("interactive_flame")

    spec = importlib.util.spec_from_file_location(
        "interactive_flame", REPO / "tools/host_profiler/interactive_flame.py"
    )
    assert spec and spec.loader
    interactive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(interactive)

    pages = {
        "report": (session / "report.html").read_text(),
        "dashboard": (session / "dashboard.html").read_text(),
        "index": html_index([]),
        "flame": flame.build_html(Path("a"), Path("b"), []),
        "interactive_flame": interactive.build_html(
            '{"name": "root", "value": 1, "children": []}', "t", "profile.speedscope.json"
        ),
    }
    stray = re.compile(r"#[0-9a-fA-F]{3,8}")
    for name, page in pages.items():
        # Assert the selector exists first: splitting it out of a page that has
        # no :root block yields "", and the literal scan below would then pass
        # without ever reading a rule.
        assert ":root{" in page, f"{name}: no :root token block to scan"
        assert f"--apm-bg:{TOKENS['bg']};" in page, f"{name}: token block missing"
        rules = page.split("}", 1)[1]
        # The :root block is the only place a literal is allowed; every other
        # rule has to read a token, or the page can drift from the others again.
        assert not stray.search(rules), f"{name}: raw color literal outside :root"

    # No page reinvents the one flat look: the base sheet declares surfaces flat
    # and the flame plot is the widest view in the product, so a radius or a
    # shadow there is decoration the other four views do not carry.
    for name, page in pages.items():
        for banned in ("border-radius", "box-shadow", "linear-gradient", "backdrop-filter"):
            assert banned not in page, f"{name}: {banned} reintroduced"


def test_dashboard_session_metadata_never_comes_from_the_analysis_block(
    tmp_path: Path,
) -> None:
    """summary.json carries two different untyped dicts: `meta` (a copy of
    meta.json) and `metadata` (the analysis block). Falling back through
    `metadata` hands the templates diagnoses where pid/utc are expected, and
    nothing raises because both are dict[str, Any]."""
    session = tmp_path / "session_meta_confusion"
    session.mkdir()
    atomic_json(session / "meta.json", _meta(pid=4242))
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": session.name,
            "metadata": {"lag_diagnosis": {"verdict": "spike-driven"}},
            "layers": [],
        },
    )
    render_session(session)
    html = (session / "dashboard.html").read_text()
    assert "pid 4242" in html


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


def test_dashboard_render_survives_non_numeric_frame_and_share(tmp_path: Path) -> None:
    """summary.json/csharp_bridge.json are re-read without schema guarantees
    (imported bundles, hand edits): a null tickIntervalAvgMs or a missing
    subsystem share must render as '?' instead of crashing the required
    render stage (Jinja's round() raises on None) and losing the dashboard."""
    session = tmp_path / "session_nullrender"
    session.mkdir()
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": session.name,
            "recommendation": "",
            "layers": [{"layer": "cpu", "score": None, "signals": {}, "optimize": []}],
            "meta": _meta(),
            "metadata": {"frame": {"gmUpdateAvgMs": None, "tickIntervalAvgMs": "junk"}},
        },
    )
    atomic_json(session / "events.json", _events(session.name, []))
    atomic_json(
        session / "csharp_bridge.json",
        {
            "bridges": [],
            "attribution": {"subsystems": [{"subsystem": "mesh", "sections": []}]},
            "stall_correlation": [],
        },
    )
    render_session(session)
    html = (session / "dashboard.html").read_text()
    # Frame numbers degraded to placeholders, not a crash. The value sits in a
    # .mono span so the figures line up column-wise, so match the markup.
    assert '<span class="mono">?</span> ms' in html
    assert "junk" not in html


def test_dashboard_missing_sections_state_instead_of_placeholders(tmp_path: Path) -> None:
    """A session with no health.json and no meta.json used to print '? ? ?' in
    the header and a lone '?' grade: a fake reading rather than a stated gap.
    The Frame/GC tile already says what is missing, so Health and the header
    line follow the same rule."""
    session = tmp_path / "session_sparse"
    session.mkdir()
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": session.name,
            "layers": [],
        },
    )
    render_session(session)
    html = (session / "dashboard.html").read_text()
    assert "No health score was produced for this session." in html
    header = html.split("<nav>", 1)[0]
    # Trailing separators from fields that were never collected.
    assert "pid ?" not in html
    assert "· ·" not in header
    assert "No frame, GC, or network samples were collected for this session." in html


def test_flame_delta_empty_result_says_so() -> None:
    """An empty delta table is indistinguishable from a broken page: name the
    result instead of leaving the reader with headers and nothing under them."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "flame_diff_empty", REPO / "tools/host_profiler/flame_diff_html.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    page = module.build_html(Path("a"), Path("b"), [])
    assert "No frames differ between these two sessions" in page
    assert 'href="report.html"' in page


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
    # finalize owns manifest.json: the run_capture path reads the verdict from
    # here rather than re-auditing (a second full SHA-256 pass over every
    # artifact) just to learn it.
    assert result.audit_valid is True
    assert (session / "manifest.json").is_file()


def test_finalize_required_stage_failure_fails_run_optional_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A required stage crashing (render) must fail the whole finalize run with
    the offending stage named; an optional stage crashing (jitsym) must only
    log and continue: enrichment is never load-bearing."""
    import apm_suite.finalize as finalize_module

    session = tmp_path / "session_stage_fail"
    session.mkdir()
    atomic_json(session / "meta.json", _meta())

    def crash(_session: Path) -> None:
        raise RuntimeError("synthetic stage crash")

    monkeypatch.setenv("SEVENDTD_APM_DIR", str(tmp_path))
    monkeypatch.setattr(finalize_module, "annotate_session", crash)  # optional stage
    monkeypatch.setattr(finalize_module, "render_session", crash)  # required stage
    result = finalize_module.finalize(session)

    assert result.failed_stages == ["render"]
    assert result.exit_code == 1
    assert "required finalization stages failed: render" in capsys.readouterr().err


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
    observed_seconds: float | None = None,
) -> Path:
    session = root / name
    session.mkdir()
    meta: dict[str, object] = {"analyzer_version": version, "only": "all", "seconds": seconds}
    if observed_seconds is not None:
        meta["observed_seconds"] = observed_seconds
    atomic_json(
        session / "summary.json",
        {
            "schema": "7dtd.apm.summary.v2",
            "session_id": name,
            "layers": [
                {"layer": layer, "score": score, "state": "collected"} for layer, score in layers
            ],
            "meta": meta,
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


def test_compare_rejects_a_truncated_capture_against_a_full_length_one(
    tmp_path: Path,
) -> None:
    """A capture cut short still records the seconds it asked for. Gating on
    that field alone lets a 6s baseline pass as a 30s one, so every rate in it
    is understated and the deltas read as a regression that never happened."""
    from apm_suite.analysis.compare import compare_sessions

    a = _cmp_session(tmp_path, "cmp_short_a", observed_seconds=6.0)
    b = _cmp_session(tmp_path, "cmp_full_b")
    with pytest.raises(ValueError, match="durations differ by more than 10%"):
        compare_sessions(a, b)
    # Two captures that both ran the same (short) window still compare.
    c = _cmp_session(tmp_path, "cmp_short_c", observed_seconds=6.0)
    assert compare_sessions(a, c)["schema"] == "7dtd.apm.compare.v2"


def test_effective_seconds_prefers_the_observed_window() -> None:
    from apm_suite.models import effective_seconds

    # Sessions written before observed_seconds existed keep the requested value.
    assert effective_seconds({"seconds": 60}) == 60
    # A full-length run that recorded its observed window is unchanged by it.
    assert effective_seconds({"seconds": 60, "observed_seconds": 60.5}) == 60
    assert effective_seconds({"seconds": 60, "observed_seconds": 12.5}) == 12.5
    # Absent, unparseable, or nonsensical observations never override it.
    assert effective_seconds({"seconds": 60, "observed_seconds": None}) == 60
    assert effective_seconds({"seconds": 60, "observed_seconds": "junk"}) == 60
    assert effective_seconds({"seconds": 60, "observed_seconds": 0}) == 60
    assert effective_seconds({}) == 0.0


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


def test_compare_marks_one_sided_attribution_not_comparable(tmp_path: Path) -> None:
    from apm_suite.analysis.compare import compare_sessions

    a = _cmp_session(tmp_path, "cmp_attr_a")
    b = _cmp_session(tmp_path, "cmp_attr_b")
    # Attribution exists only in B (no csharp_bridge.json on A): missing
    # evidence is unavailable, never an implied 0 ms that ranks as a winner.
    atomic_json(
        b / "csharp_bridge.json",
        {
            "schema": "7dtd.apm.bridge.v2",
            "attribution": {"subsystems": [{"subsystem": "network", "scaled_total_ms": 500.0}]},
        },
    )
    result = compare_sessions(a, b)
    row = next(d for d in result["attribution_deltas"] if d["subsystem"] == "network")
    assert row == {
        "subsystem": "network",
        "a_ms": 0.0,
        "b_ms": 500.0,
        "delta_b_minus_a": 500.0,
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
    partial = dict.fromkeys(("sync_locks", "runtime_gc", "cpu", "io", "memory_cache"), 20.0)
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
    "record,expected",
    [
        # A numeric string coerces, so a genuinely high sample still spikes.
        ({"t": 1.0, "cpu_pct": "999", "rss_mb": 100.0}, [("cpu_spike", 999.0)]),
        ({"t": 1.0, "cpu_pct": None, "rss_mb": 100.0}, []),
        ({"t": 1.0, "rss_mb": 100.0}, []),
        ({"t": 1.0, "cpu_pct": 200.0, "rss_mb": "big"}, [("cpu_spike", 200.0)]),
        ({"t": 1.0, "cpu_pct": 200.0, "rss_mb": None}, [("cpu_spike", 200.0)]),
        # Below the spike threshold: no event, not a zero-valued one.
        ({"t": 1.0, "cpu_pct": 12.5, "rss_mb": 100.0}, []),
    ],
)
def test_parse_proc_jsonl_survives_non_numeric_fields(
    tmp_path: Path, record: dict[str, object], expected: list[tuple[str, float]]
) -> None:
    from apm_suite.analysis.events import EventSink, parse_proc_jsonl

    session = tmp_path / "session_proc"
    (session / "memory").mkdir(parents=True)
    (session / "memory/proc.jsonl").write_text(json.dumps(record) + "\n")
    sink = EventSink()
    parse_proc_jsonl(sink, session / "memory/proc.jsonl")  # must not raise
    assert [(e["kind"], e["value"]) for e in sink.events] == expected


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


@pytest.mark.parametrize("step", [-2.0, 600.0])
def test_memory_trend_ignores_wall_clock_step_when_mono_present(
    tmp_path: Path, step: float
) -> None:
    """The rss growth rate divides elapsed time by a span; that span must come
    from the sampler's monotonic stamps when present, so an NTP/manual clock
    step mid-window (wall `t` jumping) cannot skew the leak verdict. Legacy
    sessions without mono stamps fall back to wall time (covered above)."""
    from apm_suite.analysis.report import memory_trend

    session = tmp_path / f"session_clock_step_{step}"
    _write_proc_jsonl(
        session,
        [
            {"t": 1000.0, "mono": 0.0, "rss_mb": 1000.0},
            {"t": 1000.0 + step / 2, "mono": 1.0, "rss_mb": 1000.5},
            {"t": 1000.0 + step, "mono": 2.0, "rss_mb": 1001.0},
        ],
    )
    trend = memory_trend(session)
    assert trend["rss_growth_mb_per_s"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    "wchan_top,expected",
    [
        ({"futex_wait": "5"}, [("wchan", 5)]),  # string count (coerced -> emits)
        ({"futex_wait": None}, []),  # null count (skipped)
        ({"futex_wait": 4}, [("wchan", 4)]),  # numeric passes through
        ({}, []),  # empty
        ({"futex_wait": 2}, []),  # below the waiter threshold
        ("not-a-dict", []),  # truthy non-dict degrades to "no wchan data"
    ],
)
def test_parse_threads_jsonl_survives_non_numeric_waiters(
    tmp_path: Path, wchan_top: object, expected: list[tuple[str, int]]
) -> None:
    from apm_suite.analysis.events import EventSink, parse_threads_jsonl

    session = tmp_path / "session_threads"
    (session / "threads").mkdir(parents=True)
    (session / "threads/threads.jsonl").write_text(
        json.dumps({"t": 1.0, "wchan_top": wchan_top}) + "\n"
    )
    sink = EventSink()
    parse_threads_jsonl(sink, session / "threads/threads.jsonl")  # must not raise
    assert [(e["kind"], e["value"]) for e in sink.events] == expected


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
    a = dict.fromkeys(names, 3)
    rows = delta(a, {}, top=30)
    assert [r["frame"] for r in rows] == sorted(names)
    cut = delta(a, {}, top=2)
    assert [r["frame"] for r in cut] == ["alpha", "beta"]


def test_folded_stack_path_prefers_annotated_and_accepts_legacy_layout(
    tmp_path: Path,
) -> None:
    """One resolver feeds compare and the flame diff HTML, so both must agree
    on preference order (annotated twin first) and on reading a root-level
    stacks.folded from the legacy session layout."""
    from apm_suite.analysis.flame_delta import folded_stack_path

    assert folded_stack_path(tmp_path) is None
    legacy = tmp_path / "stacks.folded"
    legacy.write_text("a 1\n")
    assert folded_stack_path(tmp_path) == legacy
    raw = tmp_path / "cpu/perf/stacks.folded"
    raw.parent.mkdir(parents=True)
    raw.write_text("b 2\n")
    assert folded_stack_path(tmp_path) == raw
    annotated = raw.with_name("stacks.annotated.folded")
    annotated.write_text("[GC] b 2\n")
    assert folded_stack_path(tmp_path) == annotated
    # Empty candidates never shadow later ones (same rule as every reader of
    # collector output here).
    annotated.write_text("")
    assert folded_stack_path(tmp_path) == raw


def test_compare_flame_deltas_read_legacy_root_level_stacks(tmp_path: Path) -> None:
    """The unified resolver keeps compare's legacy-layout support: flame frame
    deltas must build from two sessions that only carry root-level files."""
    import apm_suite.analysis.compare as compare

    for name, weight in (("cmp_legacy_a", 5), ("cmp_legacy_b", 1)):
        session = tmp_path / name
        session.mkdir()
        atomic_json(
            session / "summary.json",
            {
                "schema": "7dtd.apm.summary.v2",
                "session_id": name,
                "layers": [{"layer": "cpu", "score": 10.0, "state": "collected"}],
                "meta": {"analyzer_version": "2.1.0", "only": "all", "seconds": 30},
            },
        )
        (session / "stacks.folded").write_text(f"GameManager.gmUpdate {weight}\n")
    result = compare.compare_sessions(tmp_path / "cmp_legacy_a", tmp_path / "cmp_legacy_b")
    deltas = result["flame_frame_deltas"]
    assert len(deltas) == 1
    assert deltas[0]["frame"] == "GameManager.gmUpdate"
    assert deltas[0]["delta"] == -4


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


def test_annotate_stacks_tag_cache_is_bounded() -> None:
    # The tag memo keys on frame names, and a full-mode perf map contributes
    # hundreds of thousands of distinct ones. Left unbounded it grew with the
    # input for the whole pass; the memo must evict instead, and a name must
    # still get the same tag after it has been evicted.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "annotate_stacks_bounded", REPO / "tools/host_profiler/annotate_stacks.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.TAG_CACHE_MAX > 0
    before = module._tag_once.cache_info()
    for i in range(module.TAG_CACHE_MAX + 32):
        module.tag_frame(f"futex_wait_{i}")
    after = module._tag_once.cache_info()
    assert after.currsize <= module.TAG_CACHE_MAX, "the tag memo must be bounded"
    assert after.currsize > before.currsize
    # Eviction is a memory policy, not a result change: the same name still
    # tags the same way once it has left the cache.
    assert module.tag_frame("futex_wait_0") == "[LOCK] futex_wait_0"
    assert module.tag_frame("mono_gc_collect") == "[GC] mono_gc_collect"
    assert module.tag_frame("SomeUnmatched") == "SomeUnmatched"
    assert module.tag_frame("[GC] mono_gc_collect") == "[GC] mono_gc_collect"


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

    # Window membership is [t-5, t+5) anchored on the sample: a sample 5s
    # before a spike is outside, one 5s after it is inside, and an empty
    # spike list never matches.
    spike_ts = sorted(s for s in (12.0, 34.0))
    for t in (8.0, 16.99, 29.01, 38.99, 17.0, 39.0):
        assert module.near_spike(spike_ts, t), t
    for t in (6.99, 17.01, 23.0, 39.01, 22.0, 2.0, 7.0, 29.0):
        assert not module.near_spike(spike_ts, t), t
    assert not module.near_spike([], 12.0)


def test_correlate_load_proc_skips_torn_and_non_object_lines(tmp_path: Path) -> None:
    """proc.jsonl readers must tolerate a torn final line (a collector killed
    mid-write leaves one) and drop JSON-valid non-object lines, exactly like
    every other jsonl reader in this repo; a crash here loses the whole spike
    correlation over one bad line."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "correlate", REPO / "tools/host_profiler/correlate.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    capture = tmp_path / "session_proc"
    (capture / "memory").mkdir(parents=True)
    good = json.dumps({"t": 10.0, "cpu_pct": 90.0, "rss_mb": 8000.0})
    torn = good[:15]
    (capture / "memory/proc.jsonl").write_text(
        "\n".join([good, "", torn, "[1, 2]", good.replace("10.0", "11.0")]) + "\n",
        encoding="utf-8",
    )
    rows = list(module.iter_jsonl(module.proc_jsonl(capture)))
    assert [r["t"] for r in rows] == [10.0, 11.0]


def test_correlate_main_names_missing_proc_samples(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A capture without the memory layer is a normal input (app-only captures,
    imported bundles): correlate must exit 2 naming the paths it looked for,
    not die on a FileNotFoundError traceback from the proc.jsonl lookup."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "correlate", REPO / "tools/host_profiler/correlate.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    capture = tmp_path / "session_empty"
    capture.mkdir()
    game_log = tmp_path / "log.txt"
    game_log.write_text("", encoding="utf-8")
    original_argv = sys.argv
    try:
        sys.argv = [
            "correlate.py",
            "--capture",
            str(capture),
            "--game-log",
            str(game_log),
        ]
        assert module.main() == 2
    finally:
        sys.argv = original_argv
    err = capsys.readouterr().err
    assert "no proc samples" in err
    assert "memory/proc.jsonl" in err


def test_scenario_run_reports_unspawnable_loadgen_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """is_file() proves existence only: a loadgen script with a lost +x bit
    must fail like every other startup problem here (clean stderr message,
    exit 2), not as a PermissionError traceback out of Popen."""
    import apm_suite.cli as cli_module

    repo, store = _fake_loadgen_tree(tmp_path)
    monkeypatch.setattr(cli_module, "REPO", repo)
    monkeypatch.setattr(cli_module, "apm_root", lambda: store)

    def refuse(_argv: list[str], **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(
        cli_module,
        "subprocess",
        SimpleNamespace(Popen=refuse, TimeoutExpired=subprocess.TimeoutExpired),
    )
    result = runner.invoke(app, ["scenario", "run"])
    assert result.exit_code == 2
    assert "cannot start sibling load generator" in result.stderr


def test_compare_and_budget_name_vanished_sessions_instead_of_tracebacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session pruned between the CLI's is_file() gate and the analysis read
    raises OSError from the readers; compare and budget must report it like a
    corrupt session (named path, clean exit), matching the concurrent-prune
    contract implemented everywhere else in the suite."""
    import apm_suite.cli as cli_module

    before = tmp_path / "session_a"
    after = tmp_path / "session_b"
    for path in (before, after):
        path.mkdir()
        (path / "summary.json").write_text("{}", encoding="utf-8")

    def vanish(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", str(after / "summary.json"))

    monkeypatch.setattr(cli_module, "run_compare", vanish)
    result = runner.invoke(app, ["compare", str(before), str(after)])
    assert result.exit_code == 1
    assert "compare failed" in result.stderr

    candidate = tmp_path / "session_c"
    candidate.mkdir()
    (candidate / "summary.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_module, "check_budget", vanish)
    result = runner.invoke(app, ["budget", str(candidate)])
    assert result.exit_code == 2
    squashed = "".join(result.stderr.split())
    assert "Nosuchfileordirectory" in squashed


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


def test_app_scrape_cuts_stream_line_glued_to_a_reply_tail() -> None:
    """A streamed log line needs no newline of its own to leak player data.

    The server does not promise a stream write ends the line the command reply
    left open, so a log line can arrive glued to that reply's tail. The filter
    searches for the log timestamp rather than anchoring to the line start, so
    the player text is cut even there; the reply ahead of it still survives.
    """
    import importlib.util
    import socket
    import threading

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
                # No newline before the timestamp: the log line continues the
                # reply line the server never terminated.
                conn.sendall(
                    b"frameAvg=41.2ms"
                    b"2026-08-23T10:00:00 42.0 INF Player 'Bob' joined "
                    b"[198.51.100.9] steamid=76561198000000002\n"
                )
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

    assert "frameAvg=41.2ms" in text  # the reply ahead of the cut survives
    for leaked in ("Bob", "198.51.100.9", "76561198000000002", "INF Player"):
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
    result = runner.invoke(app, ["prune", "--keep", "3", "--max-gb", str(2500 / 1000**3)])
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


def test_terminate_tree_escalates_to_sigkill_when_sigterm_is_ignored() -> None:
    """A collector that ignores SIGTERM (wedged wrapper, root-owned tool) must
    still be reaped: the group kill escalates to SIGKILL after the bounded term
    grace instead of hanging capture shutdown forever."""
    process = subprocess.Popen(
        ["bash", "-c", "trap '' TERM; sleep 30 & wait"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    reaped = terminate_tree(process, term_grace=0.5, kill_grace=5)

    assert reaped is not None
    assert process.poll() is not None


def test_terminate_tree_escalates_when_interrupted_during_term_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt landing inside the SIGTERM grace wait must still escalate
    to the group SIGKILL before propagating: bailing out there abandons the
    whole tree (launcher + bot cohort and its sockets) with only a TERM
    delivered, which is exactly what this teardown exists to prevent."""
    process = subprocess.Popen(
        ["bash", "-c", "trap '' TERM; sleep 30 & wait"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    real_wait = process.wait
    waits = {"count": 0}

    def interrupting_wait(timeout: float | None = None) -> int:
        waits["count"] += 1
        if waits["count"] == 1:
            raise KeyboardInterrupt
        return real_wait(timeout=timeout)

    monkeypatch.setattr(process, "wait", interrupting_wait)

    with pytest.raises(KeyboardInterrupt):
        terminate_tree(process, term_grace=5)

    assert waits["count"] >= 2  # SIGKILL escalation ran despite the interrupt
    assert process.poll() is not None


def test_scenario_run_teardown_survives_second_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second Ctrl+C inside the 30s teardown grace must still run the group
    kill: skipping terminate_tree there orphans the whole bot cohort (and its
    game sockets), the exact leak the finally block exists to prevent."""
    import apm_suite.cli as cli_module

    repo, store = _fake_loadgen_tree(tmp_path)
    started: list[str] = []

    class _InterruptedLoadgen:
        pid = 424242

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            raise KeyboardInterrupt

    def fake_popen(argv: list[str], **_kwargs: object) -> _InterruptedLoadgen:
        started.append(str(argv[0]))
        return _InterruptedLoadgen()

    teardowns: list[int] = []

    def fake_terminate_tree(process: Any, **_kwargs: object) -> int:
        teardowns.append(process.pid)
        return 7

    monkeypatch.setattr(
        cli_module,
        "subprocess",
        SimpleNamespace(Popen=fake_popen, TimeoutExpired=subprocess.TimeoutExpired),
    )
    monkeypatch.setattr(cli_module, "REPO", repo)
    monkeypatch.setattr(cli_module, "apm_root", lambda: store)
    monkeypatch.setattr(
        cli_module,
        "run_capture",
        lambda **kwargs: _CaptureOutcome(_session(tmp_path / "session_scn2"), 0),
    )
    monkeypatch.setattr(cli_module, "terminate_tree", fake_terminate_tree)

    result = runner.invoke(app, ["scenario", "run"], env={"COLUMNS": "4096"})

    assert started == [str(tmp_path / "7dtd-loadgen" / "scripts" / "run_loadgen.sh")]
    assert teardowns == [424242]  # the group kill ran despite the interrupt
    assert result.exit_code == 130


def test_monitor_rotates_its_sample_log_at_the_cap(tmp_path: Path) -> None:
    """A monitor with --count 0 is a 24/7 service, so its JSONL is the only
    thing that grows. Crossing --max-bytes must keep the current file under the
    cap instead of appending forever, and must retain the previous generation
    rather than truncating the history an operator is tailing."""
    from apm_suite.cli import _rotate_monitor_log

    log = tmp_path / "monitor.jsonl"
    log.write_text("a\n" * 10, encoding="utf-8")

    _rotate_monitor_log(log, 1024)  # under the cap: untouched
    assert log.read_text(encoding="utf-8") == "a\n" * 10
    assert not (tmp_path / "monitor.jsonl.1").exists()

    _rotate_monitor_log(log, 5)  # at/over the cap: swapped out
    assert not log.exists()
    assert (tmp_path / "monitor.jsonl.1").read_text(encoding="utf-8") == "a\n" * 10

    # 0 is the documented opt-out, and a missing file must not raise.
    _rotate_monitor_log(log, 0)
    _rotate_monitor_log(tmp_path / "never-written.jsonl", 5)

    # The cap applies through the real CLI, not only to the helper.
    live = tmp_path / "live.jsonl"
    live.write_text("x\n" * 500, encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "monitor",
            "--pid",
            str(os.getpid()),
            "--count",
            "1",
            "--interval",
            "0.5",
            "--output",
            str(live),
            "--max-bytes",
            "100",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "live.jsonl.1").read_text(encoding="utf-8") == "x\n" * 500
    assert len(live.read_text(encoding="utf-8").splitlines()) == 1


def test_monitor_samples_process_and_coerces_corrupt_bridge_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 24/7 sampling loop must survive a snapshot whose numeric fields hold
    strings (coerce to numbers, never raise mid-loop), append one JSONL row per
    sample, and flag a bridge read older than 1.5 export periods as stale
    instead of presenting it as live data."""
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir()
    snapshot = {
        "update": {
            "serverTickIntervalAvgMs": "33.3",
            "lateTicks": 4,
            "gmUpdateDurationAvgMs": 12.5,
            "totalSpikes": 2,
        },
        "world": {"entities": 500, "clients": 6, "unityDeltaMs": "16.6"},
        "gc": {"gen2Collections": 3},
    }
    latest = telemetry / "apm_app_latest.json"
    latest.write_text(json.dumps(snapshot), encoding="utf-8")
    old = time.time() - 120
    os.utime(latest, (old, old))
    config = tmp_path / "Config"
    config.mkdir()
    atomic_json(config / "apmbridge.json", {"PeriodicExportSeconds": 30})
    monkeypatch.setattr("apm_suite.cli.bridge_telemetry_file", lambda _pid, name: telemetry / name)

    output = tmp_path / "monitor.jsonl"
    result = runner.invoke(
        app,
        [
            "monitor",
            "--pid",
            str(os.getpid()),
            "--count",
            "2",
            "--interval",
            "0.5",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    for row in rows:
        assert row["pid"] == os.getpid()
        assert isinstance(row["cpu_pct"], float)
        # String snapshot fields coerce to usable numbers instead of crashing.
        assert row["tps"] == pytest.approx(60.2)  # 1000 / 16.6
        assert row["tps_lifetime"] == pytest.approx(30.0)  # 1000 / 33.3
        assert row["late_ticks"] == 4 and row["full_gc"] == 3
        assert row["entities"] == 500 and row["players"] == 6
        assert row["bridge_age_s"] > 45
    squashed = _squashed(result.stdout)
    assert "cpu=" in squashed and "tps=60.2" in squashed
    # age ~120 > 30 * 1.5: every read is flagged stale on both samples.
    assert len(re.findall(r"\[bridge\d\d+\.\dsold\]", squashed)) == 2


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


def test_claim_creates_owner_only(tmp_path: Path) -> None:
    """Both claims are created owner-only in the creating syscall. A session or
    a loadgen manifest must not be group/world-readable for the window between
    creation and a later chmod, on a host with other local accounts."""
    from apm_suite.io import claim_dir, claim_file

    session = claim_dir(tmp_path / "session_20260101_000000_pid1")
    assert stat.S_IMODE(session.stat().st_mode) == 0o700
    manifest = claim_file(tmp_path / "nested" / "loadgen_123.json")
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_export_bundle_excludes_mono_bind_mount(tmp_path: Path) -> None:
    """The GC uprobes bind-mount the game's own Mono runtime onto an empty
    placeholder in runtime/. A bundle is written to be handed to a stranger, so
    that several-MB third-party binary stays on the host."""
    import zipfile

    session = tmp_path / "session_mono_mount"
    (session / "runtime").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta())
    (session / "runtime/libmonobdwgc-2.0.so").write_bytes(b"\x7fELF not evidence\n")
    (session / "runtime/mono_gc.bt.out").write_text("probe hit\n")

    bundle = tmp_path / "bundle_mono.zip"
    result = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
    assert "runtime/libmonobdwgc-2.0.so" not in names
    assert "runtime/mono_gc.bt.out" in names


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
    # The bridge mod folder rides the same override so every Python reader of
    # Mods/7dtd-server-apm-bridge resolves through one helper.
    assert paths.bridge_mod_dir() == tmp_path / "ds" / "Mods" / "7dtd-server-apm-bridge"


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

    # A grace window that is not a usable duration keeps the trash window rather
    # than clamping to 0: 0 is the documented opt-out to immediate hard deletes,
    # and a typo must not select the one setting that destroys evidence.
    for bad in ("-1", "-0.5", "nan", "inf", "-inf"):
        monkeypatch.setenv("APM_PRUNE_GRACE_HOURS", bad)
        assert prune_grace_hours() == 24.0
        assert "WARNING" in capsys.readouterr().err
    monkeypatch.setenv("APM_PRUNE_GRACE_HOURS", "6")
    assert prune_grace_hours() == 6.0
    assert capsys.readouterr().err == ""


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

    # The leak check has to run with the secret present: an unset variable
    # cannot leak, so the "set" branch would go unverified.
    monkeypatch.setenv("SEVENDTD_TELNET_PASSWORD", "apm-root-secret")
    result = inspect(None, "127.0.0.1", 8081)
    assert result["environment"]["telnet_password_set"] is True
    assert "apm-root-secret" not in json.dumps(result)


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


def test_capture_releases_jitmap_link_and_mono_mount_when_a_later_step_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /tmp perf map link and the root bind mount are host-global claims:
    the mount blocks the NEXT capture's mount and nothing ever sweeps the /tmp
    link. Both must be released even when a step after acquiring them fails, so
    the acquisition cannot sit outside the try that owns the release."""
    from apm_suite import capture

    root = tmp_path / "apm"
    root.mkdir()
    monkeypatch.setattr(capture, "apm_root", lambda: root)
    monkeypatch.setattr(capture, "_sudo_available", lambda: True)
    monkeypatch.setattr(capture, "tool_version", lambda name: "")

    link = tmp_path / "perf.map"
    target = tmp_path / "session-map"
    link.symlink_to(target)
    bound = tmp_path / "bind-target"
    unbound: list[Path] = []
    removed: list[Path] = []
    monkeypatch.setattr(capture, "_export_jitmap", lambda *a, **k: (link, target))
    monkeypatch.setattr(capture, "_bind_mono", lambda *a, **k: bound)
    monkeypatch.setattr(
        capture, "_remove_perf_map_link", lambda link_, target_: removed.append(link_)
    )
    monkeypatch.setattr(capture, "_unmount_mono", lambda m: unbound.append(m))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(capture, "_launch_collectors", boom)

    with pytest.raises(OSError):
        capture.run_capture(
            seconds=1,
            pid=os.getpid(),
            only="",
            no_app=True,
            telnet_host="",
            telnet_port=0,
            telnet_password="",
            finalize=False,
            symbolize=True,
            reset_bridge=False,
        )
    assert unbound == [bound]
    assert removed == [link]
    assert link.is_symlink()  # the stubbed release stands in for the real one


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
    # Pin the rich console width: error panels crop long session paths at the
    # default 80 columns, which depends on the pytest tmp base name
    # (/tmp/pytest-of-<user>/) and made these containment asserts flaky.
    result = runner.invoke(app, ["budget", str(session)], env={"COLUMNS": "4096"})
    assert result.exit_code == 2
    squashed = _squashed(result.stderr)
    assert str(session / "csharp_bridge.json") in squashed
    assert "cannotparse" in squashed


def test_compare_names_corrupt_bridge_file(tmp_path: Path) -> None:
    base = _cmp_session(tmp_path, "cmp_base")
    candidate = _cmp_session(tmp_path, "cmp_cand")
    (candidate / "csharp_bridge.json").write_text("[torn")
    result = runner.invoke(app, ["compare", str(base), str(candidate)], env={"COLUMNS": "4096"})
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
    result = runner.invoke(
        app, ["prometheus", str(session), "--output", str(out)], env={"COLUMNS": "4096"}
    )
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


# --- perf map link swap ------------------------------------------------------------


def test_place_perf_map_link_swaps_stale_link(tmp_path: Path) -> None:
    """pid reuse leaves the previous capture's symlink at /tmp/perf-<pid>.map;
    a fresh capture must be able to replace it with its own map."""
    map_source = tmp_path / "telemetry" / "perf-4242.map"
    map_source.parent.mkdir()
    map_source.write_text("sym 0x0\n", encoding="utf-8")
    old_source = tmp_path / "telemetry" / "perf-1111.map"
    old_source.write_text("old-process symbols\n", encoding="utf-8")
    stale = tmp_path / "perf-4242.map"
    stale.symlink_to(old_source)

    capture._place_perf_map_link(map_source, stale)

    assert stale.is_symlink()
    assert stale.resolve() == map_source.resolve()


def test_place_perf_map_link_refuses_foreign_entry(tmp_path: Path) -> None:
    """/tmp is a shared name space. A regular file (or anything that is not
    this uid's own symlink) sitting at perf-<pid>.map belongs to some other
    local user: replacing it would unlink their file and hand perf a symbol
    table this capture never wrote. The swap must refuse and leave the entry."""
    map_source = tmp_path / "telemetry" / "perf-4242.map"
    map_source.parent.mkdir()
    map_source.write_text("sym 0x0\n", encoding="utf-8")
    planted = tmp_path / "perf-4242.map"
    planted.write_text("somebody else's file\n", encoding="utf-8")

    with pytest.raises(OSError):
        capture._place_perf_map_link(map_source, planted)

    assert not planted.is_symlink()
    assert planted.read_text(encoding="utf-8") == "somebody else's file\n"
    assert not list(tmp_path.glob(".perf-4242.map.*.tmp"))  # no stranded staging link


def test_export_jitmap_survives_unplaceable_tmp_link_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unplaceable /tmp/perf-<pid>.map link (sticky-bit owner mismatch after
    pid reuse) makes the symlink swap raise; _export_jitmap itself must degrade
    to a session warning and still finish its remaining work, because perf
    would otherwise silently resolve this capture's JIT frames against the dead
    process's map."""
    session = tmp_path / "session_jitmap"
    (session / "runtime").mkdir(parents=True)
    map_source = tmp_path / "telemetry" / "perf-4242.map"
    map_source.parent.mkdir()
    map_source.write_text("sym 0x0\n", encoding="utf-8")
    monkeypatch.setattr(capture, "telnet_command", lambda *_args: True)
    monkeypatch.setattr(capture, "bridge_telemetry_file", lambda _pid, _name: map_source)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def refused(_source: Path, _link: Path) -> None:
        raise PermissionError(1, "Operation not permitted")  # sticky /tmp owner mismatch

    monkeypatch.setattr(capture, "_place_perf_map_link", refused)
    capture._export_jitmap(session, 4242, "", 0, "")

    warning = (session / "WARN.txt").read_text(encoding="utf-8")
    assert "cannot replace /tmp/perf-4242.map" in warning
    assert "WARN:" in capsys.readouterr().err
    # The failed swap must not abort the rest of the jitmap export.
    assert (session / "runtime" / "perf-4242.map").is_file()


def test_export_jitmap_skips_map_poll_when_telnet_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A jitmap command that never reached the server (telnet down, auth fail)
    means the map can never appear: the 90s growth poll must be skipped so a
    capture against an unreachable telnet does not stall before any collector."""
    session = tmp_path / "session_jitmap"
    (session / "runtime").mkdir(parents=True)
    map_source = tmp_path / "telemetry" / "perf-4242.map"  # never created
    monkeypatch.setattr(capture, "telnet_command", lambda *_args: False)
    monkeypatch.setattr(capture, "bridge_telemetry_file", lambda _pid, _name: map_source)

    def no_poll(_seconds: float) -> None:
        raise AssertionError("map poll must not run after a failed telnet send")

    monkeypatch.setattr(time, "sleep", no_poll)
    capture._export_jitmap(session, 4242, "", 0, "")

    assert "failed to send via telnet" in (session / "WARN.txt").read_text(encoding="utf-8")
    assert "WARN:" in capsys.readouterr().err


def test_export_jitmap_reports_published_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful publication must hand run_capture the (link, target) pair it
    created so the finally block can release the tmpfs name; a capture that never
    placed a link reports None and nothing is removed."""
    session = tmp_path / "session_jitmap"
    (session / "runtime").mkdir(parents=True)
    map_source = tmp_path / "telemetry" / "perf-4242.map"
    map_source.parent.mkdir()
    map_source.write_text("sym 0x0\n", encoding="utf-8")
    monkeypatch.setattr(capture, "telnet_command", lambda *_args: True)
    monkeypatch.setattr(capture, "bridge_telemetry_file", lambda _pid, _name: map_source)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    placed: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        capture, "_place_perf_map_link", lambda source, link: placed.append((source, link))
    )

    published = capture._export_jitmap(session, 4242, "", 0, "")

    assert published == (
        Path("/tmp/perf-4242.map"),
        session / "runtime" / "perf-4242.map",
    )
    assert placed == [(published[1], published[0])]

    # No placement attempt at all: nothing to release later.
    monkeypatch.setattr(capture, "telnet_command", lambda *_args: False)
    assert capture._export_jitmap(session, 4242, "", 0, "") is None
    assert len(placed) == 1


def test_remove_perf_map_link_releases_only_own_claim(tmp_path: Path) -> None:
    """Teardown removes the link only while it still points at this capture's
    target: an overlapping capture against the same pid replaces the /tmp name
    with its own map, and that replacement must survive this capture's exit."""
    ours = tmp_path / "ours.map"
    theirs = tmp_path / "theirs.map"
    ours.write_text("x\n", encoding="utf-8")
    theirs.write_text("y\n", encoding="utf-8")
    link = tmp_path / "perf-4242.map"

    # Own claim: removed.
    link.symlink_to(ours)
    capture._remove_perf_map_link(link, ours)
    assert not link.exists()

    # Overwritten by a later capture: kept.
    link.symlink_to(theirs)
    capture._remove_perf_map_link(link, ours)
    assert link.is_symlink()
    assert link.resolve() == theirs.resolve()

    # Already gone or never created: silent.
    capture._remove_perf_map_link(link, ours)  # points elsewhere: kept
    capture._remove_perf_map_link(tmp_path / "never-existed.map", tmp_path / "any.map")


# --- store races between concurrent processes ---------------------------------------


def test_list_sessions_tolerates_session_removed_by_concurrent_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session deleted by another process between glob and the sort-key stat
    must not crash every list_sessions caller (post-capture auto-prune, CLI
    prune); its mtime reads 0.0, so it sorts last and is simply gone on the
    next pass.

    The injected removal only fires where the listing stats the path. Path
    .is_dir() does not go through Path.stat (it uses os.stat), so the sort-key
    stat is the only Path.stat call on a listed session and that is where the
    race has to be provoked; the explicit mtimes below keep the expected order
    identical either way, and the mtime_or_zero assertion above covers the
    tolerance itself.
    """
    from apm_suite.session import list_sessions, mtime_or_zero

    assert mtime_or_zero(tmp_path / "never-existed") == 0.0

    (tmp_path / "session_a").mkdir()
    (tmp_path / "session_b").mkdir()
    real_is_dir = Path.is_dir
    pruned = {"done": False}
    seen_b: dict[str, int] = {"n": 0}

    def flaky_is_dir(self: Path) -> bool:
        # The race window sits between the listing's is_dir() filter and the
        # sort-key stat: report session_b as present, then remove it, so only
        # the sort-key stat finds it gone. Removing the real directory (rather
        # than counting stat calls) keeps the race reproducible: is_dir does
        # not route through Path.stat on every supported Python. The count
        # matters too: a hook that stopped firing would leave the directory in
        # place and the assertions below would still pass.
        if self == tmp_path / "session_b" and not pruned["done"]:
            pruned["done"] = True
            seen_b["n"] += 1
            self.rmdir()
            return True
        return real_is_dir(self)

    # Pin the mtimes a year apart: a filesystem with coarse timestamp
    # resolution would otherwise leave both sessions on the same sort key and
    # let readdir order decide the result.
    now = time.time()
    os.utime(tmp_path / "session_a", (now, now))
    os.utime(tmp_path / "session_b", (now - 365 * 86400, now - 365 * 86400))
    monkeypatch.setattr("apm_suite.session.Path.is_dir", flaky_is_dir)
    names = [p.name for p in list_sessions(tmp_path)]

    assert seen_b["n"] == 1
    assert names == ["session_a", "session_b"]


def test_remove_sessions_treats_already_gone_session_as_success(tmp_path: Path) -> None:
    """A concurrent prune winning the race on the same session must read as
    success (the intended end state holds), not as a scary prune failure."""
    from apm_suite.session import remove_sessions

    doomed = tmp_path / "store" / "session_gone"
    doomed.mkdir(parents=True)

    results = list(remove_sessions([doomed], grace_hours=0))

    assert results == [(doomed, None)]


def test_remove_sessions_retries_trash_name_lost_to_concurrent_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two prunes can both observe the same trash name free (the probe-then-
    rename window is not one atomic step): the loser's rename hits the winner's
    directory and must take the next suffix instead of reporting a spurious
    prune failure and leaving its session unpruned until a later run."""
    import errno as errno_module

    from apm_suite.session import remove_sessions

    store = tmp_path / "store"
    doomed = store / "session_a"
    doomed.mkdir(parents=True)
    trash = store / ".trash"
    winner = trash / "session_a"
    real_replace = os.replace
    collisions = {"n": 0}

    def colliding_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == winner:
            # The other prune's rename lands first, then ours refuses.
            winner.mkdir(exist_ok=True)
            collisions["n"] += 1
            raise OSError(errno_module.ENOTEMPTY, "Directory not empty", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr("apm_suite.session.os.replace", colliding_replace)

    results = list(remove_sessions([doomed], grace_hours=24))

    assert results == [(doomed, None)]
    assert collisions["n"] == 1
    assert winner.is_dir()
    assert (trash / "session_a_1").is_dir()
    assert not doomed.exists()


def test_remove_sessions_stamps_grace_clock_before_the_entry_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trash mtime must already be current when the rename publishes the
    entry. A capture-date mtime stamped after the rename leaves a window where a
    concurrent purge_expired_trash reads it as long expired and rmtree's the
    session inside its own grace window (and makes the stamp raise)."""
    import time as time_mod

    from apm_suite.session import remove_sessions

    store = tmp_path / "store"
    doomed = store / "session_old"
    doomed.mkdir(parents=True)
    (doomed / "summary.json").write_text("x")
    captured = 1_700_000_000  # deterministic capture date, years before the cutoff
    os.utime(doomed, (captured, captured))

    real_replace = os.replace
    seen: list[float] = []

    def observing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        # Stand in for a concurrent purge that stats the entry the instant
        # rename(2) publishes it.
        real_replace(src, dst)
        seen.append(Path(dst).stat().st_mtime)

    monkeypatch.setattr("apm_suite.session.os.replace", observing_replace)
    results = list(remove_sessions([doomed], grace_hours=24))

    assert results == [(doomed, None)]
    # Kernel file timestamps come from a coarse clock, so compare against the
    # capture date, not the wall clock read a moment ago.
    assert seen and seen[0] > captured + 3600
    assert seen[0] <= time_mod.time()
    assert (store / ".trash" / "session_old" / "summary.json").read_text() == "x"


def test_purge_expired_trash_treats_entry_gone_mid_purge_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent purge winning the race on the same trash entry (entry
    vanishes between glob and rmtree) reads as success, matching the
    remove_sessions contract instead of warning spuriously."""
    from apm_suite.session import purge_expired_trash

    entry = tmp_path / ".trash" / "session_old"
    entry.mkdir(parents=True)

    def vanishing_rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory", str(path))

    monkeypatch.setattr("apm_suite.session.shutil.rmtree", vanishing_rmtree)
    results = list(purge_expired_trash(tmp_path, grace_hours=0))

    assert results == [(entry, None)]


def test_purge_stale_scenario_runs_treats_file_gone_mid_purge_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent purge winning the race on the same loadgen file (file
    vanishes between glob and unlink) reads as success, not a failure."""
    from apm_suite.session import purge_stale_scenario_runs

    entry = tmp_path / ".scenario" / "loadgen_123.json"
    entry.parent.mkdir(parents=True)
    entry.write_text("{}", encoding="utf-8")

    def vanishing_unlink(self: Path, missing_ok: bool = False) -> None:
        raise FileNotFoundError(2, "No such file or directory", str(self))

    monkeypatch.setattr("apm_suite.session.Path.unlink", vanishing_unlink)
    results = list(purge_stale_scenario_runs(tmp_path, grace_hours=0))

    assert results == [(entry, None)]


def test_place_perf_map_link_swap_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /tmp link swap must never pass through an absent state: an
    overlapping capture polls perf-<pid>.map continuously, and a reader that
    lands between unlink and symlink records its whole window unresolved.
    os.replace must observe the old link still live at the swap instant."""
    from apm_suite.capture import _place_perf_map_link

    old_target = tmp_path / "old.map"
    old_target.write_text("old", encoding="utf-8")
    new_target = tmp_path / "new.map"
    new_target.write_text("new", encoding="utf-8")
    link = tmp_path / "perf-1.map"
    link.symlink_to(old_target)
    observed: list[bool] = []
    real_replace = os.replace

    def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        observed.append(Path(dst).is_symlink())
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    _place_perf_map_link(new_target, link)

    assert os.readlink(link) == str(new_target)
    assert observed == [True]  # old link present at the swap instant
    # no staging temp left behind
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "new.map",
        "old.map",
        "perf-1.map",
    ]


def test_place_perf_map_link_cleans_up_when_swap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An impossible swap (sticky-dir owner rules, simulated here) raises
    OSError to the caller for its warning and leaves no staging temp."""
    from apm_suite.capture import _place_perf_map_link

    target = tmp_path / "map"
    target.write_text("m", encoding="utf-8")
    link = tmp_path / "perf-1.map"

    def refusing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise PermissionError(1, "Operation not permitted", str(dst))

    monkeypatch.setattr(os, "replace", refusing_replace)
    with pytest.raises(OSError):
        _place_perf_map_link(target, link)

    assert not link.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["map"]


def test_index_scan_skips_session_unreadable_mid_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A summary.json removed between iterdir and its read (concurrent prune)
    must skip the row like any other unreadable summary, never crash index."""
    import apm_suite.analysis.index as index_mod

    session = tmp_path / "session_x"
    session.mkdir()
    atomic_json(
        session / "summary.json",
        {"schema": "7dtd.apm.summary.v2", "session_id": "session_x", "layers": []},
    )

    def vanishing_read(path: Path) -> dict[str, Any]:
        raise FileNotFoundError(2, "No such file or directory", str(path))

    monkeypatch.setattr(index_mod, "load_json", vanishing_read)
    assert index_mod.scan(tmp_path) == []


# --- monitor / bridge / matrix error contracts ---------------------------------------


def test_monitor_reports_access_denied_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server owned by another user raises psutil.AccessDenied on the first
    sample: end with exit 2 and a fix, never a bare traceback."""

    def denied(self: psutil.Process, interval: float | None = None) -> float:
        raise psutil.AccessDenied(pid=self.pid)

    monkeypatch.setattr(psutil.Process, "cpu_percent", denied)
    result = runner.invoke(app, ["monitor", "--pid", str(os.getpid()), "--count", "1"])

    assert result.exit_code == 2
    assert "access denied" in result.stderr


def test_bridge_command_reports_unreadable_summary(tmp_path: Path) -> None:
    """A malformed summary.json surfaces as an operator error naming the file
    (same contract as compare/budget), never a JSONDecodeError traceback."""
    session = tmp_path / "session_bad"
    session.mkdir()
    (session / "summary.json").write_text("{not json", encoding="utf-8")

    result = runner.invoke(app, ["bridge", str(session)])

    assert result.exit_code == 1
    assert "bridge analysis failed" in result.stderr


def test_scenario_matrix_rejects_mistyped_entry_value_before_any_run(
    tmp_path: Path,
) -> None:
    """Plan entries bypass Typer's parsing on the direct scenario_run call, so
    a mistyped value ("seconds": "60") must be rejected naming entry+field
    BEFORE cleanup/loadgen side effects, not crash mid-matrix with TypeError."""
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"seconds": "60"}, {"seconds": 30}]), encoding="utf-8")

    result = runner.invoke(app, ["scenario", "matrix", str(plan)])

    assert result.exit_code == 2
    assert "entry 1 field 'seconds': expected int, got '60'" in result.stderr


def test_scenario_matrix_routes_telnet_target_to_cleanup_and_every_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix telnet target drives the between-experiment cleanup and every
    experiment's own telnet traffic. A hardcoded 127.0.0.1:8081 made the flags
    on `scenario run` inert and cleaned a different install than the one under
    test, contaminating the next experiment silently."""
    from apm_suite import cli

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"label": "one"}]), encoding="utf-8")
    telnet_calls: list[tuple[object, ...]] = []

    def record_telnet(*args: object) -> bool:
        telnet_calls.append(args)
        return True

    def record_run(**kwargs: object) -> None:
        seen.append(kwargs)

    monkeypatch.setenv("SEVENDTD_TELNET_PASSWORD", "pw")
    # Patch the binding `scenario_matrix` calls, not capture's: cli imports
    # telnet_command by value, so patching the capture module leaves the
    # command's own reference untouched, the real socket call stays in place,
    # and the cleanup is never recorded.
    monkeypatch.setattr(cli, "telnet_command", record_telnet)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "scenario_run", record_run)

    result = runner.invoke(
        app,
        [
            "scenario",
            "matrix",
            str(plan),
            "--telnet-host",
            "10.0.0.5",
            "--telnet-port",
            "9999",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert telnet_calls == [("10.0.0.5", 9999, "pw", "killall")]
    assert seen == [
        {
            "game_port": 26902,
            "telnet_host": "10.0.0.5",
            "telnet_port": 9999,
            "label": "one",
        }
    ]


def test_scenario_commands_expose_the_telnet_target() -> None:
    """`--telnet-host/--telnet-port` must exist on both scenario commands and
    default from the one settings module. A literal in either command is the
    drift this guards: a flag that is accepted and then ignored sends the
    capture at one install while the operator believes it hit another."""
    from apm_suite import cli
    from apm_suite.settings import DEFAULT_TELNET_HOST, DEFAULT_TELNET_PORT

    signature = inspect.signature(cli.scenario_run)
    assert signature.parameters["telnet_host"].default == DEFAULT_TELNET_HOST
    assert signature.parameters["telnet_port"].default == DEFAULT_TELNET_PORT
    matrix = inspect.signature(cli.scenario_matrix)
    assert matrix.parameters["telnet_host"].default == DEFAULT_TELNET_HOST
    assert matrix.parameters["telnet_port"].default == DEFAULT_TELNET_PORT

    for command in (cli.scenario_run, cli.scenario_matrix):
        for name, parameter in inspect.signature(command).parameters.items():
            if name.startswith("telnet_"):
                assert parameter.default is not inspect.Parameter.empty, (
                    f"{command.__name__}: {name} has no default"
                )

    source = Path(inspect.getfile(cli)).read_text(encoding="utf-8")
    body = source.split("def scenario_matrix", 1)[0].split("def scenario_run", 1)[1]
    assert "127.0.0.1" not in body and ", 8081" not in body
    matrix_body = source.split("def scenario_matrix", 1)[1]
    assert "127.0.0.1" not in matrix_body and ", 8081" not in matrix_body


# --- error-path hardening (resilience audit) ----------------------------------------


def test_export_jitmap_survives_vanished_map_at_symbol_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The map can vanish between the poll and the final symbol-count read
    (server died, telemetry cleaned up). That count is a cosmetic headline:
    it must degrade to a stderr note, never abort run_capture BEFORE its
    signal/collector block and lose the whole window."""
    session = tmp_path / "session_jitmap"
    (session / "runtime").mkdir(parents=True)
    map_source = tmp_path / "telemetry" / "perf-4242.map"
    map_source.parent.mkdir()
    map_source.write_text("sym 0x0\n", encoding="utf-8")
    monkeypatch.setattr(capture, "telnet_command", lambda *_args: True)
    monkeypatch.setattr(capture, "bridge_telemetry_file", lambda _pid, _name: map_source)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def place_then_target_dies(_source: Path, _link: Path) -> None:
        map_source.unlink()  # server exited; telemetry dir removed

    monkeypatch.setattr(capture, "_place_perf_map_link", place_then_target_dies)
    capture._export_jitmap(session, 4242, "", 0, "")

    assert "symbol count unavailable" in capsys.readouterr().err


def test_export_jitmap_survives_vanished_map_at_publish_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The map can vanish between the poll's last successful stat and the
    final publish re-check (telemetry cleaned up between the two calls).
    That re-check runs BEFORE run_capture installs its signal handler or
    launches any collector, so an unhandled FileNotFoundError there aborts
    the whole capture over a cosmetic headline."""
    session = tmp_path / "session_jitmap"
    (session / "runtime").mkdir(parents=True)
    real_map = tmp_path / "telemetry" / "perf-4242.map"
    real_map.parent.mkdir()
    real_map.write_text("sym 0x0\n", encoding="utf-8")
    monkeypatch.setattr(capture, "telnet_command", lambda *_args: True)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    class VanishingMap:
        """Present through the growth poll, gone by the publish re-check."""

        def __init__(self, backing: Path) -> None:
            self.backing = backing
            self.stats = 0

        def stat(self) -> os.stat_result:
            self.stats += 1
            if self.stats > 2:  # two stable poll passes, then removed
                raise FileNotFoundError(2, "No such file or directory", str(self.backing))
            return self.backing.stat()

        def is_file(self) -> bool:
            return True  # it existed a moment ago; removal lands before the re-stat

    vanishing = VanishingMap(real_map)
    monkeypatch.setattr(capture, "bridge_telemetry_file", lambda _pid, _name: vanishing)
    capture._export_jitmap(session, 4242, "", 0, "")

    assert "jitmap export failed; managed perf frames stay [jit]" in (
        session / "WARN.txt"
    ).read_text(encoding="utf-8")
    assert "WARN:" in capsys.readouterr().err


def test_import_bundle_corrupt_member_cleans_partial_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member whose stored bytes fail the CRC check raises BadZipFile (not
    OSError) mid-extract. The partial-import cleanup must cover it too, or a
    torn bundle strands a half-restored session that later audits INVALID."""
    import zipfile

    session = tmp_path / "session_badmember"
    (session / "io").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta())
    (session / "io/vfs.bt.out").write_text("openat /steamapps/common\n")
    bundle = tmp_path / "session_badmember.zip"
    exported = runner.invoke(app, ["export", str(session), "--output", str(bundle)])
    assert exported.exit_code == 0, exported.output

    # Flip one byte inside the second member's stored data so extraction of
    # meta.json succeeds first and vfs.bt.out then fails CRC verification.
    with zipfile.ZipFile(bundle) as archive:
        infos = archive.infolist()
        victim = next(info for info in infos if info.filename.endswith(".bt.out"))
        data_start = victim.header_offset + 30 + len(victim.filename) + len(victim.extra)
    raw = bytearray(bundle.read_bytes())
    raw[data_start] ^= 0xFF
    bundle.write_bytes(raw)

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(store))
    result = runner.invoke(app, ["import", str(bundle)])

    assert result.exit_code == 2
    assert "removed partial import" in result.stderr
    assert not list(store.glob("session_*"))


def test_memory_trend_skips_non_numeric_records(tmp_path: Path) -> None:
    """A junk record (string t/rss_mb from a hand-edited or imported jsonl)
    must drop out of the trend instead of raising float(ValueError) out of
    the required summary stage; good records still produce the slope."""
    from apm_suite.analysis.report import memory_trend

    session = tmp_path / "session_junk_proc"
    _write_proc_jsonl(
        session,
        [
            {"t": "early", "rss_mb": 999.0},  # non-numeric stamp
            {"t": 1.0, "rss_mb": "huge"},  # non-numeric rss
            {"t": 1.0, "rss_mb": 1000.0},
            {"t": 2.0, "rss_mb": 1000.5},
            {"t": 3.0, "rss_mb": 1001.0},
        ],
    )
    trend = memory_trend(session)
    assert trend["rss_growth_mb_per_s"] == pytest.approx(0.5)


def test_diagnose_lag_tolerates_junk_snapshot_scalars() -> None:
    """Bridge snapshot extra=allow blocks and re-read summaries are not
    schema-guarded: string/container scalars must coerce to 'no data' instead
    of raising int()/float() errors through the required summary stage."""
    from apm_suite.analysis.report import diagnose_lag

    metadata = {
        "frame": {
            "lateTicks": "many",
            "windowUpdates": {"n": 5},
            "tickIntervalAvgMs": None,
            "gmUpdateAvgMs": "busy",
        },
        "gc": {"allocMBPerSecond": [1], "grossAllocMBPerSecond": "high"},
    }
    result = diagnose_lag([], metadata, {})
    assert result["laggy"] is False
    assert result["verdict"] == "server met its tick deadline this window"


def test_verify_recorded_hashes_records_unreadable_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact that cannot be read (perms, vanished mid-audit under a
    concurrent prune) is itself an integrity finding; crashing would lose the
    report for every other recorded artifact too."""
    from apm_suite.session import verify_recorded_hashes

    session = tmp_path / "session_locked"
    (session / "io").mkdir(parents=True)
    (session / "io/vfs.bt.out").write_text("evidence\n")

    def unreadable(path: Path) -> str:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("apm_suite.session.file_sha256", unreadable)
    artifact = Artifact(
        path="io/vfs.bt.out",
        bytes=(session / "io/vfs.bt.out").stat().st_size,
        sha256="0" * 64,
    )
    manifest = ManifestV2(
        session_id="session_locked",
        started_at=datetime.now(UTC),
        target=Target(pid=1),
        requested_layers=["all"],
        artifacts=[artifact],
    )
    atomic_json(session / "manifest.json", schema_dict(manifest))

    errors = verify_recorded_hashes(session)

    assert len(errors) == 1
    assert "unreadable" in errors[0]
    assert "io/vfs.bt.out" in errors[0]


def test_audit_manifest_walk_skips_file_gone_mid_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file pruned by a concurrent process between rglob and its hash must
    be skipped (same contract as _mtime), not crash every overlapping audit."""
    from apm_suite.io import file_sha256 as real_sha256

    hashed: list[Path] = []

    def vanishing_after_first(path: Path) -> str:
        if hashed:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        hashed.append(path)
        return real_sha256(path)

    monkeypatch.setattr("apm_suite.session.file_sha256", vanishing_after_first)
    session = tmp_path / "session_race"
    (session / "io").mkdir(parents=True)
    atomic_json(session / "meta.json", _meta())
    (session / "io/a.txt").write_text("a\n")
    (session / "io/b.txt").write_text("b\n")

    manifest, _valid = audit_session(session)

    # Only the artifact hashed before the race is recorded; the rest are
    # skipped, not fatal, and never recorded with a fabricated hash. The
    # remaining manifest errors are the fixture's own absent files, none of
    # them a hashing failure.
    assert len(hashed) == 1
    recorded = {artifact.path for artifact in manifest.artifacts}
    assert recorded == {hashed[0].relative_to(session).as_posix()}
    assert "io/b.txt" not in recorded
    assert not any("FileNotFound" in error for error in manifest.errors)


def test_doctor_bridge_hash_failure_reports_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bridge DLL owned by another user cannot be hashed; doctor must report
    the failed check with a fix, never crash the whole readiness report."""
    from apm_suite import doctor

    ds = tmp_path / "ds"
    mods = ds / "Mods" / "7dtd-server-apm-bridge"
    mods.mkdir(parents=True)
    (mods / "7dtd-server-apm-bridge.dll").write_bytes(b"installed")
    repo = tmp_path / "repo"
    built_dir = repo / "dist" / "7dtd-server-apm-bridge"
    built_dir.mkdir(parents=True)
    (built_dir / "7dtd-server-apm-bridge.dll").write_bytes(b"built")
    monkeypatch.setenv("SEVENDTD_DS_DIR", str(ds))
    monkeypatch.setattr(doctor, "REPO", repo)

    def denied(path: Path) -> str:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(doctor, "file_sha256", denied)

    result = doctor._bridge_status()

    assert result["ok"] is False
    assert "cannot hash" in (result["fix"] or "")


def test_check_budget_missing_budget_file_raises_not_silent(tmp_path: Path) -> None:
    """check_budget's contract: a custom budget path must never fall back to
    DEFAULT_BUDGET silently - a missing file raises naming the path (the CLI
    pre-check exists, but the library owns the guarantee)."""
    from apm_suite.analysis.budget import check_budget

    session = tmp_path / "session_budget"
    session.mkdir()

    with pytest.raises(ValueError, match="does not exist"):
        check_budget(session, tmp_path / "absent.json")


def test_annotate_stream_error_leaves_no_partial_annotated_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed annotation pass must not strand a PARTIAL .annotated.txt:
    readers prefer that file whenever it exists, so a truncated twin would
    shadow the raw evidence with less content than the original."""
    from apm_suite.analysis import jitsym

    source = tmp_path / "probe.bt.out"
    source.write_text(
        "@alloc[\n 0xffffffff81000000 Foo.bar\n]: 10\n@x 0xdeadbeef tail\n",
        encoding="utf-8",
    )
    target = tmp_path / "probe.annotated.txt"

    def flaky_resolver(_starts: object, _entries: object) -> Any:
        state = {"n": 0}

        def resolve(match: re.Match[str]) -> str:
            # First substitution succeeds (target file is opened and written),
            # then the pass dies mid-file like an unreadable source would.
            state["n"] += 1
            if state["n"] >= 2:
                raise OSError("read failed mid-pass")
            return match.group(0)

        return resolve

    monkeypatch.setattr(jitsym, "_resolver", flaky_resolver)

    with pytest.raises(OSError):
        jitsym._annotate_stream(
            source, target, [0xFFFFFFFF81000000], [(0xFFFFFFFF81000010, "Foo.bar")]
        )

    assert not target.exists()


# --- runner echo --------------------------------------------------------------------


def test_run_echo_survives_markup_like_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    """run() echoes every command it executes; bracketed argument text must
    survive the rich echo literally (the old unescaped print raised
    MarkupError for a stray closing tag BEFORE subprocess.run, so nothing
    executed at all)."""
    from apm_suite import runner as runner_mod

    rc = runner_mod.run(["true", "--mode", "x[green]y[/dim]"])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "[/dim]" in printed  # rendered as text, not consumed as markup


# --- scaling determinism -------------------------------------------------------------


def test_scaling_tied_exponents_rank_deterministically(tmp_path: Path) -> None:
    from apm_suite.analysis.scaling import analyze_scaling

    # Every section fits the same exponent (all linear), so the rounded sort
    # key ties everywhere; name order (not per-process set order) must decide.
    sessions = []
    for n in (100, 200, 400):
        s = tmp_path / f"session_n{n}"
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
                "top_managed_sections": [
                    {"name": f"Sect{c}", "avgMs": float(10 - c), "totalMs": float(10 - c) * n}
                    for c in range(10)
                ],
            },
        )
        sessions.append(s)
    result = analyze_scaling(sessions, "players")
    names = [f["section"] for f in result["sections"]]
    assert names == sorted(names)


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


def test_audit_manifest_is_replay_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auditing the same session bytes twice must produce the same manifest.

    The window stamps describe the capture, so they come from meta.json. Stamping
    them from the auditing host's wall clock made every replay of a session
    differ, which defeats diffing two captures artifact by artifact.
    """
    from apm_suite import session as session_mod

    first = _session(tmp_path / "session_replay_a")
    audit_session(first)
    recorded = (first / "manifest.json").read_bytes()

    class _LateClock(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> _LateClock:
            return cls(2031, 7, 8, 9, 10, 11, tzinfo=UTC)

    monkeypatch.setattr(session_mod, "datetime", _LateClock)
    audit_session(first)
    assert (first / "manifest.json").read_bytes() == recorded

    manifest = ManifestV2.model_validate(load_json(first / "manifest.json"))
    assert manifest.started_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert manifest.ended_at == datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)


def test_audit_records_unknown_start_instead_of_the_auditing_clock(tmp_path: Path) -> None:
    """A meta.json without a usable stamp is missing evidence, not a fresh one."""
    meta = _meta()
    meta["utc"] = "not-a-timestamp"
    broken = _session(tmp_path / "session_replay_b")
    atomic_json(broken / "meta.json", meta)

    manifest, _ = audit_session(broken)
    assert manifest.started_at is None
    assert manifest.ended_at is None
    assert "capture start time is unknown" in " ".join(manifest.warnings)
    assert ManifestV2.model_validate(load_json(broken / "manifest.json")).started_at is None
