from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .analysis.bridge import analyze
from .analysis.budget import check_budget
from .analysis.compare import run_compare
from .analysis.index import write_index
from .capture import find_server_pid, run_capture, write_plan_text
from .doctor import inspect
from .finalize import finalize as finalize_session
from .io import atomic_json, atomic_text, load_json
from .paths import APM_BACKENDS, REPO, apm_root
from .runner import backend_python, run
from .session import audit_session

app = typer.Typer(help="Host-only APM for 7 Days to Die dedicated servers.", no_args_is_help=True)
flame_app = typer.Typer(help="Build and compare flame profiles.", no_args_is_help=True)
scenario_app = typer.Typer(
    help="Run an APM capture under sibling load generation.", no_args_is_help=True
)
app.add_typer(flame_app, name="flame")
app.add_typer(scenario_app, name="scenario")
console = Console()


def _exit(code: int) -> None:
    if code:
        raise typer.Exit(code)


@app.callback()
def root(version: Annotated[bool, typer.Option("--version", help="Show version.")] = False) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit()


@app.command()
def doctor(
    pid: Annotated[int | None, typer.Option(help="Server process ID.")] = None,
    telnet_host: Annotated[str, typer.Option()] = "127.0.0.1",
    telnet_port: Annotated[int, typer.Option()] = 8081,
    strict: Annotated[bool, typer.Option()] = False,
    json_output: Annotated[Path | None, typer.Option("--json")] = None,
) -> None:
    result = inspect(pid, telnet_host, telnet_port)
    if json_output:
        atomic_json(json_output, result)
    for layer, available in result["available_layers"].items():
        console.print(f"[green]OK[/green] {layer}" if available else f"[yellow]--[/yellow] {layer}")
    for name, check in (result.get("checks") or {}).items():
        if isinstance(check, dict) and not check.get("ok") and check.get("fix"):
            console.print(f"[yellow]![/yellow] {name}: {check['fix']}")
    _exit(1 if strict and not result["ready"] else 0)


@app.command()
def capture(
    seconds: Annotated[int, typer.Option(min=1)] = 45,
    pid: Annotated[int | None, typer.Option()] = None,
    only: Annotated[str, typer.Option(help="Comma-separated collector names.")] = "all",
    no_app: Annotated[bool, typer.Option()] = False,
    telnet_host: Annotated[str, typer.Option(help="Server telnet host.")] = "127.0.0.1",
    telnet_port: Annotated[int, typer.Option(help="Server telnet port.")] = 8081,
    telnet_password: Annotated[
        str,
        typer.Option(
            "--telnet-password",
            envvar="SEVENDTD_TELNET_PASSWORD",
            help="Prefer the environment variable to avoid shell history.",
        ),
    ] = "",
    reset_bridge: Annotated[
        bool, typer.Option(help="Reset bridge stats at capture start (window-scoped totals).")
    ] = False,
    symbolize: Annotated[
        bool,
        typer.Option(
            help="Export the JIT map so managed frames resolve to method names "
            "(pre-window burst). Disable only for a faster, unresolved capture."
        ),
    ] = True,
    dry_run: Annotated[bool, typer.Option(help="Print the resolved collector plan.")] = False,
) -> None:
    if dry_run:
        console.print(
            write_plan_text(
                {"seconds": seconds, "pid": pid, "no_app": no_app, "telnet": telnet_host}, only
            )
        )
        return
    try:
        outcome = run_capture(
            seconds=seconds,
            pid=pid,
            only=only,
            no_app=no_app,
            telnet_host=telnet_host,
            telnet_port=telnet_port,
            telnet_password=telnet_password,
            reset_bridge=reset_bridge,
            symbolize=symbolize,
        )
    except RuntimeError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(2) from None
    console.print(f"APM session: {outcome.session}")
    _exit(outcome.exit_code)


@app.command()
def finalize(session: Path, skip_bridge: Annotated[bool, typer.Option()] = False) -> None:
    if not session.is_dir():
        console.print(f"[red]not a session directory: {session}[/red]")
        raise typer.Exit(2)
    _exit(finalize_session(session, skip_bridge=skip_bridge).exit_code)


@app.command()
def audit(session: Path, strict: Annotated[bool, typer.Option()] = False) -> None:
    if not session.is_dir():
        console.print(f"[red]not a session directory: {session}[/red]")
        raise typer.Exit(2)
    manifest, valid = audit_session(session)
    console.print(
        f"audit: {'valid' if valid else 'INVALID'}; "
        f"{len(manifest.errors)} errors, {len(manifest.warnings)} warnings"
    )
    _exit(1 if not valid or (strict and manifest.warnings) else 0)


@app.command()
def index(root: Annotated[Path | None, typer.Option()] = None) -> None:
    count = write_index(root)
    console.print(f"indexed {count} sessions -> {(root or apm_root()) / 'index.html'}")


# Keys that carry the raw launch command / binary path. Redacted wherever they
# appear in any JSON doc, at any depth - so summary.json (which embeds the whole
# meta dict) and any future meta-embedding file are covered without a per-filename
# allowlist that silently regresses.
_REDACT_KEYS = {"cmdline", "exe"}


def _scrub(obj: object) -> object:
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if k in _REDACT_KEYS and isinstance(v, str) else _scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


@app.command("export")
def export_session(session: Path, output: Annotated[Path, typer.Option("--output", "-o")]) -> None:
    """Create a sanitized support bundle without raw command lines or telnet text."""
    if not session.is_dir():
        raise typer.BadParameter("session directory does not exist")
    excluded = {"perf.data", "bridge.jsonl", "FINALIZE.txt"}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build under a temp path in the destination dir and os.replace on success, so
    # a malformed input never truncates the target or clobbers a prior bundle.
    fd, tmp_zip = tempfile.mkstemp(suffix=".zip", dir=output.parent)
    os.close(fd)
    tmp_zip_path = Path(tmp_zip)
    try:
        with (
            tempfile.TemporaryDirectory() as tmp,
            zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as archive,
        ):
            temp = Path(tmp)
            for source in session.rglob("*"):
                if not source.is_file() or source.name in excluded or source.suffix == ".err":
                    continue
                relative = source.relative_to(session)
                if source.suffix == ".json":
                    try:
                        data = json.loads(source.read_text())
                    except (json.JSONDecodeError, OSError) as error:
                        raise typer.BadParameter(f"cannot parse {relative}: {error}") from None
                    sanitized = temp / relative
                    sanitized.parent.mkdir(parents=True, exist_ok=True)
                    sanitized.write_text(json.dumps(_scrub(data), indent=2) + "\n")
                    archive.write(sanitized, relative)
                else:
                    archive.write(source, relative)
        os.replace(tmp_zip_path, output)
    finally:
        tmp_zip_path.unlink(missing_ok=True)
    console.print(f"sanitized bundle: {output}")


@app.command("scaling")
def scaling(
    sessions: Annotated[list[Path], typer.Argument(help="Finalized sessions from a scale ladder.")],
    by: Annotated[str, typer.Option(help="Scale variable: players or entities.")] = "players",
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Detect super-linear code: fit each managed section's cost vs load across a
    ladder of captures and rank by scaling exponent (O(N^exp), worst first)."""
    from .analysis.scaling import analyze_scaling

    usable = [s for s in sessions if (s / "summary.json").is_file()]
    if len(usable) < 3:
        console.print(
            "[red]need >= 3 finalized sessions (different load levels) to fit scaling[/red]"
        )
        raise typer.Exit(2)
    result = analyze_scaling(usable, scale_key=by)
    distinct = sorted(set(result["scales"]))
    if len(distinct) < 3:
        console.print(
            f"[red]sessions span only {len(distinct)} distinct {by} value(s) ({distinct}); "
            f"a log-log fit needs >= 3 distinct load levels. Capture at different {by} counts "
            "(e.g. via plans/profile.scale-ladder.json).[/red]"
        )
        raise typer.Exit(2)
    if output:
        atomic_json(output, result)
    console.print(f"scale ({by}): {result['scales']}")
    console.print(f"{'section':44s} {'per-call':>10s} {'total':>10s}  class")
    for f in result["sections"][:25]:
        pc = f["per_call_exponent"]
        tot = f["total_exponent"]
        console.print(
            f"{f['section']:44s} {(pc if pc is not None else '-'):>10} "
            f"{(tot if tot is not None else '-'):>10}  "
            f"call:{f['per_call_class']} total:{f['total_class']}"
        )
    if result["super_linear"]:
        console.print(f"\n[yellow]super-linear ({len(result['super_linear'])}):[/yellow]")
        for f in result["super_linear"]:
            console.print(
                f"  {f['section']} - per-call O(N^{f['per_call_exponent']}), "
                f"total O(N^{f['total_exponent']})"
            )
    else:
        console.print("\nno super-linear sections detected")


def _prom_label(value: object) -> str:
    """Escape a Prometheus label value per spec (\\ then " then newline). Current
    label sources are fixed internal names, but a metrics exporter must never
    emit a value that could break the line format."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@app.command("prometheus")
def prometheus(session: Path, output: Annotated[Path, typer.Option("--output", "-o")]) -> None:
    """Export finalized, coverage-aware layer metrics in Prometheus text format."""
    summary_path = session / "summary.json"
    if not summary_path.is_file():
        raise typer.BadParameter("session has no summary.json")
    summary = json.loads(summary_path.read_text())
    lines = [
        "# HELP sevendtd_apm_layer_pressure Layer pressure from a collected APM layer.",
        "# TYPE sevendtd_apm_layer_pressure gauge",
    ]
    for layer in summary.get("layers") or []:
        score = layer.get("score")
        # A non-finite score (inf/nan) must never reach the scrape line.
        if layer.get("state") == "collected" and score is not None and math.isfinite(float(score)):
            name = _prom_label(layer.get("layer", "unknown"))
            lines.append(
                f'sevendtd_apm_layer_pressure{{layer="{name}"}} {float(score):.6f}'
            )
    health_path = session / "health.json"
    health = load_json(health_path) if health_path.is_file() else summary.get("health") or {}
    if health.get("coverage") is not None:
        lines += [
            "# TYPE sevendtd_apm_coverage gauge",
            f"sevendtd_apm_coverage {float(health['coverage']):.6f}",
        ]
    bridge_path = session / "csharp_bridge.json"
    if bridge_path.is_file():
        attribution = load_json(bridge_path).get("attribution") or {}
        subsystems = attribution.get("subsystems") or []
        if subsystems:
            lines += [
                "# HELP sevendtd_apm_subsystem_ms Window-scoped managed time per subsystem.",
                "# TYPE sevendtd_apm_subsystem_ms gauge",
            ]
            for entry in subsystems:
                subsystem = entry.get("subsystem")
                scaled = entry.get("scaled_total_ms")
                if subsystem is None or scaled is None:
                    continue
                name = _prom_label(subsystem)
                lines.append(
                    f'sevendtd_apm_subsystem_ms{{subsystem="{name}"}} {float(scaled):.3f}'
                )
    lag = (summary.get("metadata") or {}).get("lag_diagnosis") or {}
    if lag:
        lines += [
            "# HELP sevendtd_apm_laggy 1 when the server missed its tick deadline.",
            "# TYPE sevendtd_apm_laggy gauge",
            f"sevendtd_apm_laggy {1 if lag.get('laggy') else 0}",
        ]
        causes = lag.get("causes") or []
        if causes:
            lines += [
                "# HELP sevendtd_apm_lag_cause_severity Per-cause lag severity (0-1).",
                "# TYPE sevendtd_apm_lag_cause_severity gauge",
            ]
            for cause in causes:
                name = _prom_label(cause.get("cause", "unknown"))
                lines.append(
                    f'sevendtd_apm_lag_cause_severity{{cause="{name}"}} '
                    f"{float(cause.get('severity') or 0):.3f}"
                )
    frame = (summary.get("metadata") or {}).get("frame") or {}
    if frame.get("lateTicks") is not None:
        lines += [
            "# TYPE sevendtd_apm_late_ticks gauge",
            f"sevendtd_apm_late_ticks {int(frame['lateTicks'])}",
        ]
    gc_meta = (summary.get("metadata") or {}).get("gc") or {}
    if gc_meta.get("allocMBPerSecond") is not None:
        lines += [
            "# TYPE sevendtd_apm_alloc_mb_per_second gauge",
            f"sevendtd_apm_alloc_mb_per_second {float(gc_meta['allocMBPerSecond']):.3f}",
        ]
    if gc_meta.get("grossAllocMBPerSecond") is not None:
        lines += [
            "# TYPE sevendtd_apm_gross_alloc_mb_per_second gauge",
            f"sevendtd_apm_gross_alloc_mb_per_second {float(gc_meta['grossAllocMBPerSecond']):.3f}",
        ]
    gc_layer = next(
        (
            layer.get("signals") or {}
            for layer in (summary.get("layers") or [])
            if layer.get("layer") == "runtime_gc"
        ),
        {},
    )
    if gc_layer.get("stw_pause_worst_ms") is not None:
        lines += [
            "# TYPE sevendtd_apm_gc_stw_worst_ms gauge",
            f"sevendtd_apm_gc_stw_worst_ms {float(gc_layer['stw_pause_worst_ms']):.3f}",
            "# TYPE sevendtd_apm_gc_stw_total_ms gauge",
            f"sevendtd_apm_gc_stw_total_ms {float(gc_layer.get('stw_pause_total_ms') or 0):.3f}",
        ]
    # Kernel UDP send is the honest windowed chunk rate (bridge transfers is a
    # join-burst-weighted lifetime average; see report R56).
    net_meta = (summary.get("metadata") or {}).get("net") or {}
    if net_meta.get("udp_send_mb_per_second") is not None:
        lines += [
            "# TYPE sevendtd_apm_udp_send_mb_per_second gauge",
            f"sevendtd_apm_udp_send_mb_per_second {float(net_meta['udp_send_mb_per_second']):.3f}",
        ]
    # Atomic write: a scrape racing the export must not read a truncated file.
    atomic_text(output, "\n".join(lines) + "\n")
    console.print(f"Prometheus metrics: {output}")


@app.command()
def monitor(
    pid: Annotated[int | None, typer.Option()] = None,
    interval: Annotated[float, typer.Option(min=0.5)] = 5,
    count: Annotated[int, typer.Option(min=0, help="Samples to take; 0 = until Ctrl+C.")] = 0,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Append JSONL here.")
    ] = None,
) -> None:
    """Continuously sample process and bridge health without a full capture."""
    import psutil

    if pid is None:
        pid = find_server_pid()
    if pid is None or not psutil.pid_exists(pid):
        console.print("[red]no unique running 7DaysToDieServe process; pass --pid[/red]")
        raise typer.Exit(2)
    process = psutil.Process(pid)
    bridge_latest = (
        Path(os.path.realpath(f"/proc/{pid}/exe")).parent
        / "Mods/7dtd-apm-bridge/telemetry/apm_app_latest.json"
    )
    taken = 0
    previous_late: int | None = None
    previous_gc: int | None = None
    try:
        while count == 0 or taken < count:
            with process.oneshot():
                cpu = process.cpu_percent(interval=interval)
                sample: dict[str, object] = {
                    "t": time.time(),
                    "pid": pid,
                    "cpu_pct": round(cpu, 1),
                    "rss_mb": round(process.memory_info().rss / 1048576, 1),
                    "threads": process.num_threads(),
                }
            if bridge_latest.is_file():
                try:
                    snapshot = json.loads(bridge_latest.read_text())
                    update = snapshot.get("update") or {}
                    world = snapshot.get("world") or {}
                    sample["tick_avg_ms"] = update.get("serverTickIntervalAvgMs")
                    sample["late_ticks"] = update.get("lateTicks")
                    sample["gm_update_avg_ms"] = update.get("gmUpdateDurationAvgMs")
                    sample["spikes"] = update.get("totalSpikes")
                    sample["entities"] = world.get("entities")
                    sample["players"] = world.get("clients") or world.get("players")
                    # TPS from the tick interval; the honest health headline (20 =
                    # target, well under = falling behind). Survives when telnet
                    # is too saturated to answer, unlike a listplayers poll.
                    tick = update.get("serverTickIntervalAvgMs") or 0
                    sample["tps"] = round(1000 / tick, 1) if tick else None
                    # Each full (gen2) collection is a Boehm stop-the-world pause.
                    sample["full_gc"] = (snapshot.get("gc") or {}).get("gen2Collections")
                    # The bridge exports every PeriodicExportSeconds (default 30);
                    # flag samples older than that so stale reads are not mistaken
                    # for live data.
                    sample["bridge_age_s"] = round(time.time() - bridge_latest.stat().st_mtime, 1)
                except (json.JSONDecodeError, OSError):
                    pass
            current_late = sample.get("late_ticks")
            late_delta = ""
            if isinstance(current_late, int) and isinstance(previous_late, int):
                delta = current_late - previous_late
                late_delta = f" late+{delta}" if delta > 0 else " late=0"
            if isinstance(current_late, int):
                previous_late = current_late
            bridge_age = sample.get("bridge_age_s")
            stale = (
                f"  [bridge {bridge_age}s old]"
                if isinstance(bridge_age, float) and bridge_age > interval * 1.5
                else ""
            )
            current_gc = sample.get("full_gc")
            gc_delta = ""
            if isinstance(current_gc, int) and isinstance(previous_gc, int):
                d = current_gc - previous_gc
                gc_delta = f" fullGC+{d}(STW!)" if d > 0 else " fullGC=0"
            if isinstance(current_gc, int):
                previous_gc = current_gc

            def _ms(value: object) -> str:
                return f"{value:.1f}" if isinstance(value, (int, float)) else "-"

            tps = sample.get("tps")
            tps_str = f"{tps:.1f}" if isinstance(tps, (int, float)) else "-"
            console.print(
                f"cpu={sample['cpu_pct']:6.1f}%  rss={sample['rss_mb']:8.1f}MB  "
                f"threads={sample['threads']}  tps={tps_str}  "
                f"tick={_ms(sample.get('tick_avg_ms'))}ms  gm={_ms(sample.get('gm_update_avg_ms'))}ms  "
                f"ent={sample.get('entities', '-')}  ply={sample.get('players', '-')}  "
                f"spikes={sample.get('spikes', '-')}{late_delta}{gc_delta}{stale}"
            )
            if output:
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("a") as stream:
                    stream.write(json.dumps(sample) + "\n")
            taken += 1
    except (KeyboardInterrupt, psutil.NoSuchProcess):
        console.print("monitor stopped")


@app.command("prune")
def prune_sessions(
    keep: Annotated[int, typer.Option(min=1)] = 20,
    max_gb: Annotated[
        float | None,
        typer.Option(help="Total size budget; oldest sessions removed until under it."),
    ] = None,
    dry_run: Annotated[bool, typer.Option()] = False,
) -> None:
    root = apm_root()
    sessions = sorted(
        (p for p in root.glob("session_*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    doomed = list(sessions[keep:])
    if max_gb is not None:
        budget = max_gb * 1024**3
        sizes = {p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) for p in sessions}
        total = sum(sizes.values()) - sum(sizes[p] for p in doomed)
        for session in reversed(sessions[:keep]):  # oldest kept first
            if total <= budget:
                break
            if session not in doomed:
                doomed.append(session)
                total -= sizes[session]
    for old in doomed:
        console.print(("would remove " if dry_run else "removing ") + str(old))
        if not dry_run:
            shutil.rmtree(old)


@app.command()
def compare(
    before: Path, after: Path, output: Annotated[Path | None, typer.Option()] = None
) -> None:
    try:
        run_compare(before, after, output)
    except (ValueError, FileNotFoundError) as error:
        console.print(f"[red]compare failed: {error}[/red]")
        raise typer.Exit(1) from None


@app.command()
def budget(
    session: Path,
    budget_file: Annotated[Path | None, typer.Option("--budget")] = None,
    baseline: Annotated[Path | None, typer.Option()] = None,
    max_regression: Annotated[float, typer.Option()] = 15,
) -> None:
    if not (session / "summary.json").is_file():
        console.print(f"[red]missing summary.json in {session}[/red]")
        raise typer.Exit(2)
    _exit(0 if check_budget(session, budget_file, baseline, max_regression) else 1)


@app.command()
def bridge(
    session: Path,
    snapshot: Annotated[
        Path | None, typer.Option(help="Optional in-game APM bridge snapshot.")
    ] = None,
) -> None:
    if not session.is_dir():
        console.print(f"[red]not a session directory: {session}[/red]")
        raise typer.Exit(2)
    result = analyze(session, snapshot)
    console.print(result["playbook_md"])
    console.print(f"wrote {session / 'csharp_bridge.json'}")


@scenario_app.command("run")
def scenario_run(
    seconds: Annotated[int, typer.Option(min=1)] = 45,
    clients: Annotated[int, typer.Option(min=1)] = 6,
    actions: Annotated[int, typer.Option(min=1)] = 500,
    seed: Annotated[
        int, typer.Option(help="Bot action RNG seed (fixed = reproducible cohort behaviour).")
    ] = 42,
    game_port: Annotated[int, typer.Option()] = 26902,
    pid: Annotated[int | None, typer.Option()] = None,
    preset: Annotated[
        str,
        typer.Option(
            help="standard, deep, or forensic (forensic adds the mono_alloc probe "
            "for gross-allocation churn + STW attribution: use it to diagnose GC lag)"
        ),
    ] = "standard",
    bot_mode: Annotated[
        str, typer.Option(help="wander, mixed, demolition, combat, chaos, kite, traverse")
    ] = "",
    bot_mix: Annotated[
        str,
        typer.Option(
            help="Weighted per-bot mode mix, e.g. 'traverse:35,combat:20,bait:15' "
            "(heterogeneous cohort; overrides --bot-mode). See the 'canonical' profile."
        ),
    ] = "",
    spawn_entity: Annotated[str, typer.Option(help="Telnet-spawned entity class(es).")] = "",
    spawn_per_player: Annotated[int, typer.Option(min=0)] = 0,
    spawn_every_ms: Annotated[int, typer.Option(min=0)] = 0,
    horde_every_ms: Annotated[
        int, typer.Option(min=0, help="Wandering-horde burst cadence (0 = off).")
    ] = 0,
    horde_waves: Annotated[int, typer.Option(min=1, help="Scout waves per horde target.")] = 3,
    max_dynamite: Annotated[int, typer.Option(min=0)] = 0,
    no_spawn: Annotated[bool, typer.Option(help="Disable telnet zombie pressure.")] = False,
    warmup: Annotated[
        int, typer.Option(min=0, help="Seconds of load before the capture window starts.")
    ] = 0,
    rally: Annotated[
        bool,
        typer.Option(help="After warmup, teleport all players together (small chunk union)."),
    ] = False,
    rally_at: Annotated[
        str,
        typer.Option(help="Rally to fresh coordinates 'x,z' (avoids gore-saturated spawn grid)."),
    ] = "",
    reset_bridge: Annotated[
        bool, typer.Option(help="Reset bridge stats at capture start (window-scoped totals).")
    ] = True,
    label: Annotated[str, typer.Option(help="Experiment label stored in workload.json.")] = "",
    telnet_password: Annotated[str, typer.Option(envvar="SEVENDTD_TELNET_PASSWORD")] = "",
) -> None:
    presets = {"standard": "app,threads,memory,cpu", "deep": "all", "forensic": "all,alloc"}
    if preset not in presets:
        console.print("[red]preset must be standard, deep, or forensic[/red]")
        raise typer.Exit(2)
    loadgen = REPO.parent / "7dtd-loadgen" / "scripts" / "run_loadgen.sh"
    if not loadgen.is_file():
        console.print(f"[red]sibling load generator not found: {loadgen}[/red]")
        raise typer.Exit(2)
    run_dir = apm_root() / ".scenario"
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    workload = run_dir / f"loadgen_{stamp}.json"
    stats = run_dir / f"loadgen_{stamp}_stats.json"
    env = os.environ.copy()
    env.update(
        {
            "LOADGEN_MODE": "join",
            "LOADGEN_COUNT": str(clients),
            "LOADGEN_ACTIONS": str(actions),
            "LOADGEN_PORT": str(game_port),
            "LOADGEN_TIMEOUT": str((warmup + seconds + 30) * 1000),
            "LOADGEN_MANIFEST": str(workload),
            "LOADGEN_STATS_JSON": str(stats),
        }
    )
    optional_env = {
        "LOADGEN_SEED": str(seed),
        "LOADGEN_BOT_MODE": bot_mode,
        "LOADGEN_BOT_MIX": bot_mix,
        "LOADGEN_SPAWN_ENTITY": spawn_entity,
        "LOADGEN_SPAWN_PER_PLAYER": str(spawn_per_player) if spawn_per_player else "",
        "LOADGEN_SPAWN_EVERY_MS": str(spawn_every_ms) if spawn_every_ms else "",
        "LOADGEN_HORDE_EVERY_MS": str(horde_every_ms) if horde_every_ms else "",
        "LOADGEN_HORDE_WAVES": str(horde_waves) if horde_every_ms else "",
        "LOADGEN_MAX_DYNAMITE": str(max_dynamite) if max_dynamite else "",
        "LOADGEN_NO_SPAWN": "1" if no_spawn else "",
    }
    env.update({key: value for key, value in optional_env.items() if value})
    console.print(
        f"starting sibling 7dtd-loadgen: clients={clients} actions={actions} "
        f"mode={bot_mode or 'auto'} warmup={warmup}s"
    )
    # Validate rally_at BEFORE starting bots so a typo fails fast with no leaked
    # subprocess and no wasted warmup.
    coordinates: tuple[int, int] | None = None
    if rally_at:
        try:
            x_str, z_str = rally_at.split(",")
            coordinates = (int(x_str), int(z_str))
        except ValueError as error:
            raise typer.BadParameter(
                f"--rally-at expects 'x,z' (two integers), got {rally_at!r}"
            ) from error
    load_process = subprocess.Popen([str(loadgen)], env=env)
    session: Path | None = None
    capture_rc = 130
    load_rc = 130
    try:
        # Inside the try so the finally always tears the loadgen down, even if
        # warmup/rally raises (else the bot cohort would leak).
        if warmup:
            console.print(f"warmup: waiting {warmup}s for join + spawn steady state")
            time.sleep(warmup)
        if rally or rally_at:
            from .capture import rally_players

            moved = rally_players("127.0.0.1", 8081, telnet_password, at=coordinates)
            console.print(f"rally: teleported {moved} players into one cluster")
            time.sleep(15 if rally_at else 10)  # let teleport chunk churn settle
        outcome = run_capture(
            seconds=seconds,
            pid=pid,
            only=presets[preset],
            no_app=False,
            telnet_host="127.0.0.1",
            telnet_port=8081,
            telnet_password=telnet_password,
            reset_bridge=reset_bridge,
        )
        session = outcome.session
        capture_rc = outcome.exit_code
    except RuntimeError as error:
        console.print(f"[red]{error}[/red]")
        capture_rc = 2
    except KeyboardInterrupt:
        # Ctrl-C: still tear the loadgen down (finally) and report a session.
        capture_rc = 130
    finally:
        # Deterministic loadgen shutdown even when the capture is interrupted.
        try:
            load_rc = load_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            load_process.terminate()
            try:
                load_rc = load_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                load_process.kill()
                # Bounded: an uninterruptible-sleep child must not hang shutdown
                # forever; leave it to the OS if it will not die.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    load_rc = load_process.wait(timeout=5)
                # Reap it if it exited right after the SIGKILL so we don't leave a
                # zombie behind (a still-D-state child can't be reaped by anyone).
                load_process.poll()
    if session is not None and workload.is_file():
        doc = json.loads(workload.read_text())
        if label:
            doc["label"] = label
        doc.setdefault("workload", {})["botMode"] = bot_mode or doc.get("workload", {}).get(
            "botMode", "auto"
        )
        atomic_json(session / "workload.json", doc)
        if stats.is_file():
            shutil.copy2(stats, session / "loadgen_stats.json")
        audit_session(session)
        console.print(f"workload manifest attached: {session / 'workload.json'}")
    _exit(capture_rc or load_rc)


@scenario_app.command("matrix")
def scenario_matrix(
    plan: Path,
    game_port: Annotated[int, typer.Option()] = 26902,
    cleanup: Annotated[
        str, typer.Option(help="Console command run between experiments ('' disables).")
    ] = "killall",
    telnet_password: Annotated[str, typer.Option(envvar="SEVENDTD_TELNET_PASSWORD")] = "",
) -> None:
    """Run a labeled experiment sequence from a JSON plan (list of scenario kwargs)."""
    from .capture import telnet_command

    entries = json.loads(plan.read_text())
    if not isinstance(entries, list) or not entries:
        console.print("[red]plan must be a non-empty JSON list of experiment objects[/red]")
        raise typer.Exit(2)
    allowed = {
        "seconds",
        "clients",
        "actions",
        "seed",
        "preset",
        "bot_mode",
        "bot_mix",
        "spawn_entity",
        "spawn_per_player",
        "spawn_every_ms",
        "horde_every_ms",
        "horde_waves",
        "max_dynamite",
        "no_spawn",
        "warmup",
        "rally",
        "rally_at",
        "reset_bridge",
        "label",
    }
    results: list[tuple[str, int]] = []
    for position, entry in enumerate(entries, 1):
        unknown = set(entry) - allowed
        if unknown:
            console.print(f"[red]plan entry {position} has unknown keys: {sorted(unknown)}[/red]")
            raise typer.Exit(2)
        label = str(entry.get("label") or f"experiment-{position}")
        if cleanup:
            telnet_command("127.0.0.1", 8081, telnet_password, cleanup)
            time.sleep(8)
        console.print(f"[bold]=== matrix {position}/{len(entries)}: {label}[/bold]")
        code = 0
        try:
            scenario_run(
                game_port=game_port,
                telnet_password=telnet_password,
                **{**entry, "label": label},
            )
        except typer.Exit as stop:
            code = stop.exit_code or 0
        results.append((label, code))
    for label, code in results:
        console.print(f"  {label}: exit={code}")
    _exit(0 if all(code in (0, 1) for _, code in results) else 1)


@flame_app.command("build")
def flame_build(directory: Path) -> None:
    _exit(run([str(REPO / "tools/host_profiler/make_flames.sh"), str(directory)], check=False))


@flame_app.command("diff")
def flame_diff(before: Path, after: Path) -> None:
    _exit(backend_python(APM_BACKENDS / "flame_diff_html.py", [str(before), str(after)]))


if __name__ == "__main__":
    app()
