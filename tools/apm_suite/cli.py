from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from . import __version__
from .analysis.bridge import analyze
from .analysis.budget import check_budget
from .analysis.compare import run_compare
from .analysis.index import write_index
from .bundle import BundleError, export_bundle, import_bundle
from .capture import (
    bridge_telemetry_file,
    find_server_pid,
    run_capture,
    write_plan_text,
)
from .collectors import unknown_only_tokens
from .io import (
    atomic_json,
    claim_file,
    force_utf8_stdio,
    load_jsonc,
    write_stdout,
)
from .models import as_number
from .paths import REPO, apm_root, require_backends
from .prometheus import MetricError, export_metrics
from .runner import backend_python, run, terminate_tree
from .session import (
    MISSING_PREFIX,
    audit_session,
    list_sessions,
    prune_grace_hours,
    prune_store,
    sessions_beyond_budget,
    verify_session,
)

# Bridge config bounds, mirroring BridgeConfig.cs. The bridge clamps
# PeriodicExportSeconds to [0, 3600] and treats 0 as "never export"; this
# reader keeps 0 as unusable and falls back to the default, so it never keys
# the monitor's stale flag off a cadence the mod will not produce.
DEFAULT_BRIDGE_EXPORT_SECONDS = 30.0
MAX_BRIDGE_EXPORT_SECONDS = 3600.0

app = typer.Typer(help="Host-only APM for 7 Days to Die dedicated servers.", no_args_is_help=True)
flame_app = typer.Typer(help="Build and compare flame profiles.", no_args_is_help=True)
scenario_app = typer.Typer(
    help="Run an APM capture under sibling load generation.", no_args_is_help=True
)
app.add_typer(flame_app, name="flame")
app.add_typer(scenario_app, name="scenario")


console = Console()
err_console = Console(stderr=True)


class CapturePreset(StrEnum):
    """Collector sets accepted by `scenario run --preset`."""

    STANDARD = "standard"
    DEEP = "deep"
    FORENSIC = "forensic"


class ScaleBy(StrEnum):
    """Load variables accepted by `scaling --by`."""

    PLAYERS = "players"
    ENTITIES = "entities"


# A monitor with --count 0 runs until killed, so its JSONL is the only thing it
# ever produces and the only thing that grows. A default interval of 5s writes
# ~17k lines/day, so without a cap a 24/7 service fills the volume on its own.
# One generation is kept: an operator tailing the file finds the previous run
# in <output>.1 rather than an unexplained gap.
MONITOR_MAX_BYTES = 64 * 1024**2


def _rotate_monitor_log(path: Path, max_bytes: int) -> None:
    """Rename a full sample log to <path>.1 so the next append starts empty.

    Runs before the write, so the live file never exceeds the cap by more than
    one record. A stat/unlink/replace failure leaves the log exactly as it was:
    an unwritable directory is the operator's problem to see, not a reason to
    drop samples.
    """
    if max_bytes <= 0:
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return  # no log yet: the first append creates it
    previous = path.with_name(path.name + ".1")
    with suppress(OSError):
        previous.unlink(missing_ok=True)
        os.replace(path, previous)


def _exit(code: int) -> None:
    if code:
        raise typer.Exit(code)


def _require_backends() -> None:
    """CLI boundary for paths.require_backends: clean error instead of a traceback."""
    try:
        require_backends()
    except RuntimeError as error:
        err_console.print(f"[red]{escape(str(error))}[/red]")
        raise typer.Exit(2) from None


def _require_session_dir(session: Path) -> None:
    if not session.is_dir():
        err_console.print(f"[red]not a session directory: {escape(str(session))}[/red]")
        raise typer.Exit(2)


def _version_callback(value: bool) -> None:
    if value:
        print(__version__)
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Host-only APM for 7 Days to Die dedicated servers.

    Every command runs with UTF-8 stdout/stderr, whatever LANG says. The pin
    happens here, before the command body, so a path, hostname, or tool
    version with a non-ASCII character is printed rather than raising
    UnicodeEncodeError out of the printer. See apm_suite.io.force_utf8_stdio.
    """
    force_utf8_stdio()


@app.command()
def doctor(
    pid: Annotated[
        int | None,
        typer.Option(help="Server process ID; auto-detects the unique server when omitted."),
    ] = None,
    telnet_host: Annotated[str, typer.Option(help="Server telnet host.")] = "127.0.0.1",
    telnet_port: Annotated[int, typer.Option(help="Server telnet port.")] = 8081,
    strict: Annotated[
        bool, typer.Option(help="Exit 1 instead of 0 when the host is not ready.")
    ] = False,
    json_output: Annotated[
        Path | None,
        typer.Option("--json", help="Write the full report as JSON here ('-' = stdout)."),
    ] = None,
) -> None:
    """Check host readiness for each APM capture layer."""
    # Lazy like the other command-scoped imports: doctor is the only psutil
    # consumer, and a module-level import would tax every CLI invocation.
    from .doctor import inspect

    result = inspect(pid, telnet_host, telnet_port)
    if json_output == Path("-"):
        write_stdout(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return
    if json_output:
        atomic_json(json_output, result)
    for layer, available in result["available_layers"].items():
        console.print(f"[green]OK[/green] {layer}" if available else f"[yellow]--[/yellow] {layer}")
    # Every populated fix prints, not only failed checks: _bridge_status sets a
    # fix on a healthy bridge when DeepMode is off, and the README promises
    # doctor flags that. Fix text embeds OS/telnet error strings (user-set
    # hostnames, paths), so escape it like every other untrusted echo.
    for name, check in (result.get("checks") or {}).items():
        if isinstance(check, dict) and check.get("fix"):
            marker = "[yellow]![/yellow]" if not check.get("ok") else "[blue]*[/blue]"
            console.print(f"{marker} {name}: {escape(str(check['fix']))}")
    _exit(1 if strict and not result["ready"] else 0)


@app.command()
def capture(
    seconds: Annotated[int, typer.Option(min=1, help="Capture duration in seconds.")] = 45,
    pid: Annotated[
        int | None,
        typer.Option(help="Server process ID; auto-detects the unique server when omitted."),
    ] = None,
    only: Annotated[str, typer.Option(help="Comma-separated collector names or layers.")] = "all",
    no_app: Annotated[
        bool,
        typer.Option(
            "--no-app/--app",
            help="Collect the app/telnet layer. --no-app skips it (and the "
            "bridge) for kernel-only captures.",
        ),
    ] = False,
    telnet_host: Annotated[str, typer.Option(help="Server telnet host.")] = "127.0.0.1",
    telnet_port: Annotated[int, typer.Option(help="Server telnet port.")] = 8081,
    reset_bridge: Annotated[
        bool, typer.Option(help="Reset bridge stats at capture start (window-scoped totals).")
    ] = False,
    symbolize: Annotated[
        bool,
        typer.Option(
            help="Export the full JIT map so managed frames resolve to method names. "
            "The map burst runs on the server's MAIN thread and can freeze a loaded "
            "server for tens of seconds - default OFF so a capture against a "
            "production server is safe; pass --symbolize for bench/flamegraph runs."
        ),
    ] = False,
    dry_run: Annotated[bool, typer.Option(help="Print the resolved collector plan.")] = False,
) -> None:
    """Run a timed collector session against the server and finalize it."""
    # Secret via environment only (AGENTS.md rule 4): a CLI flag would land the
    # password in shell history and this process's /proc/<pid>/cmdline.
    telnet_password = os.environ.get("SEVENDTD_TELNET_PASSWORD", "")
    if unknown := unknown_only_tokens(only):
        # The tokens are raw argv; a stray closing tag would otherwise raise
        # MarkupError out of the printer instead of reaching this exit-2 hint.
        err_console.print(
            f"[red]unknown --only value(s): {escape(', '.join(unknown))}; "
            "use collector names or layers as listed by 'capture --dry-run'[/red]"
        )
        raise typer.Exit(2)
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
        # Message embeds user-set hostnames/paths; escape like every other echo.
        err_console.print(f"[red]{escape(str(error))}[/red]")
        raise typer.Exit(2) from None
    console.print(f"APM session: {escape(str(outcome.session))}")
    _exit(outcome.exit_code)


@app.command()
def finalize(
    session: Annotated[Path, typer.Argument(help="Raw session directory to finalize.")],
    skip_bridge: Annotated[
        bool, typer.Option(help="Skip the managed bridge correlation stage.")
    ] = False,
) -> None:
    """Run finalization stages and write summary artifacts for a raw session."""
    _require_session_dir(session)
    # Deferred so commands that never finalize (audit, prune, monitor, export...)
    # skip the finalize chain's reporting/jinja2 import at startup.
    from .finalize import finalize as finalize_session

    _exit(finalize_session(session, skip_bridge=skip_bridge).exit_code)


@app.command()
def audit(
    session: Annotated[Path, typer.Argument(help="Session directory to verify.")],
    strict: Annotated[
        bool, typer.Option(help="Exit 1 when warnings are present too, not only errors.")
    ] = False,
) -> None:
    """Verify session artifact integrity against recorded hashes."""
    _require_session_dir(session)
    manifest, valid = audit_session(session, verify_recorded=True)
    console.print(
        f"audit: {'valid' if valid else 'INVALID'}; "
        f"{len(manifest.errors)} errors, {len(manifest.warnings)} warnings"
    )
    # An INVALID verdict without the offending paths names nothing to fix; list
    # every error (missing file, failed schema, recorded-hash mismatch).
    # Each error quotes untrusted content (imported-bundle artifact paths,
    # schema-validation input values), so escape it: rich would otherwise
    # interpret square brackets in those strings as console markup.
    for error in manifest.errors:
        err_console.print(f"[red]{escape(error)}[/red]")
    _exit(1 if not valid or (strict and manifest.warnings) else 0)


# Verdict labels, aligned for readable store listings.
_VERDICT_WIDTH = 11
_STORE_EXTRAS = (".scenario", ".trash", "index.html")


@app.command("verify-store")
def verify_store(
    store: Annotated[
        Path | None,
        typer.Argument(help="Store directory to verify (default: the APM session store)."),
    ] = None,
    strict: Annotated[
        bool, typer.Option(help="Exit 1 for incomplete sessions too, not only invalid ones.")
    ] = False,
) -> None:
    """Audit every session in a store read-only (restore drill for a copied-back store).

    `audit` re-stamps manifest.json on a clean session, so it cannot answer
    whether a restored copy is intact: the re-stamp absorbs the very drift the
    check exists to find. This command never writes, so it can be pointed at a
    copy pulled back from backup and its verdict trusted.
    """
    root = store or apm_root()
    if not root.is_dir():
        err_console.print(f"[red]not a store directory: {escape(str(root))}[/red]")
        raise typer.Exit(2)
    sessions = list_sessions(root)
    ok = incomplete = invalid = 0
    for session in sessions:
        errors = verify_session(session)
        if not (session / "manifest.json").is_file():
            errors.append("no manifest.json recorded; artifact hashes unverified")
        # A session whose only findings are absent required documents or a
        # never-recorded manifest is a capture still running (or one copied
        # before finalize), not corruption: it is reported, and only --strict
        # fails on it.
        unverified = bool(errors) and all(
            error.startswith((MISSING_PREFIX, "no manifest.json")) for error in errors
        )
        if not errors:
            ok += 1
            label = "ok"
        elif unverified:
            incomplete += 1
            label = "incomplete"
        else:
            invalid += 1
            label = "INVALID"
        console.print(f"{label:>{_VERDICT_WIDTH}}  {escape(session.name)}")
        for error in errors:
            err_console.print(f"{' ' * _VERDICT_WIDTH}  {escape(error)}")
    # Non-session store state the copy must carry too: loadgen manifests under
    # .scenario and the soft-delete window under .trash are both evidence.
    present = [name for name in _STORE_EXTRAS if (root / name).exists()]
    console.print(
        f"verified {len(sessions)} session(s) in {escape(str(root))}: "
        f"{ok} ok, {incomplete} incomplete, {invalid} invalid"
    )
    if present:
        console.print("store entries present: " + ", ".join(present))
    else:
        console.print("no .scenario, .trash, or index.html in this store")
    _exit(1 if invalid or (strict and incomplete) else 0)


@app.command()
def index(
    root: Annotated[
        Path | None, typer.Option(help="Sessions directory (default: the APM data root).")
    ] = None,
) -> None:
    """Write the HTML session index over all finalized sessions."""
    count = write_index(root)
    console.print(f"indexed {count} sessions -> {escape(str((root or apm_root()) / 'index.html'))}")


@app.command("export")
def export_session(
    session: Annotated[Path, typer.Argument(help="Finalized session directory to bundle.")],
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Zip file to write (must not be a directory).")
    ],
) -> None:
    """Create a sanitized support bundle without raw command lines or telnet text."""
    try:
        written = export_bundle(session, output)
    except BundleError as error:
        raise typer.BadParameter(str(error)) from None
    console.print(f"sanitized bundle: {escape(str(written))}")


@app.command("import")
def import_bundle_command(
    bundle: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="Zip support bundle."),
    ],
    store: Annotated[
        Path | None,
        typer.Option("--store", help="Session store root (default: the APM session store)."),
    ] = None,
) -> None:
    """Restore an exported support bundle into the session store and audit it."""
    store_root = store or apm_root()
    try:
        result = import_bundle(bundle, store_root)
    except BundleError as error:
        err_console.print(f"[red]{escape(str(error))}[/red]")
        raise typer.Exit(2) from None
    outcome = (
        "audit passed"
        if result.valid
        else f"{result.errors} error(s), {result.warnings} warning(s)"
    )
    console.print(f"restored {escape(str(result.session))} ({outcome})")
    # A restored session is evidence in the store: an existing index must list
    # it instead of waiting for the next capture or a manual `index`. A store
    # that was never indexed stays unindexed (import writes no index files).
    if (store_root / "index.json").is_file():
        write_index(store_root)


@app.command("scaling")
def scaling(
    sessions: Annotated[list[Path], typer.Argument(help="Finalized sessions from a scale ladder.")],
    by: Annotated[ScaleBy, typer.Option(help="Load variable to fit against.")] = ScaleBy.PLAYERS,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write the full ranking as JSON here.")
    ] = None,
) -> None:
    """Rank managed sections by load-scaling exponent across a session ladder.

    Fits each section's cost vs load (players or entities) on a log-log fit and
    ranks by exponent (O(N^exp), worst first). Needs 3+ finalized sessions at
    distinct load levels.
    """
    from .analysis.scaling import analyze_scaling

    usable = [s for s in sessions if (s / "summary.json").is_file()]
    if len(usable) < 3:
        err_console.print(
            "[red]need >= 3 finalized sessions (different load levels) to fit scaling[/red]"
        )
        raise typer.Exit(2)
    result = analyze_scaling(usable, scale_key=by.value)
    distinct = sorted(set(result["scales"]))
    if len(distinct) < 3:
        err_console.print(
            f"[red]sessions span only {len(distinct)} distinct {by.value} value(s) ({distinct}); "
            f"a log-log fit needs >= 3 distinct load levels. Capture at different {by.value} "
            "counts (e.g. via plans/profile.scale-ladder.json).[/red]"
        )
        raise typer.Exit(2)
    if output:
        atomic_json(output, result)
    # Section names come from csharp_bridge.json (imported bundles are
    # untrusted); escape them so bracketed names cannot render as markup.
    console.print(f"scale ({by.value}): {escape(str(result['scales']))}")
    console.print(f"{'section':44s} {'per-call':>10s} {'total':>10s}  class")
    for f in result["sections"][:25]:
        pc = f["per_call_exponent"]
        tot = f["total_exponent"]
        console.print(
            f"{escape(f['section']):44s} {(pc if pc is not None else '-'):>10} "
            f"{(tot if tot is not None else '-'):>10}  "
            f"call:{f['per_call_class']} total:{f['total_class']}"
        )
    if result["super_linear"]:
        console.print(f"\n[yellow]super-linear ({len(result['super_linear'])}):[/yellow]")
        for f in result["super_linear"]:
            console.print(
                f"  {escape(f['section'])} - per-call O(N^{f['per_call_exponent']}), "
                f"total O(N^{f['total_exponent']})"
            )
    else:
        console.print("\nno super-linear sections detected")


@app.command("prometheus")
def prometheus(
    session: Annotated[Path, typer.Argument(help="Finalized session directory to export.")],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Metrics text file to write (e.g. a .prom file)."),
    ],
) -> None:
    """Export finalized, coverage-aware layer metrics in Prometheus text format."""
    try:
        export_metrics(session, output)
    except MetricError as error:
        raise typer.BadParameter(str(error)) from None
    console.print(f"Prometheus metrics: {escape(str(output))}")


@app.command()
def monitor(
    pid: Annotated[
        int | None,
        typer.Option(help="Server process ID; auto-detects the unique server when omitted."),
    ] = None,
    interval: Annotated[float, typer.Option(min=0.5, help="Seconds between samples.")] = 5,
    count: Annotated[int, typer.Option(min=0, help="Samples to take; 0 = until Ctrl+C.")] = 0,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Append JSONL here, ~1 line per interval. Capped by --max-bytes; "
            "at the cap the current file becomes <output>.1 and a fresh one starts.",
        ),
    ] = None,
    max_bytes: Annotated[
        int,
        typer.Option(
            "--max-bytes",
            min=0,
            help=f"Cap on --output in bytes; 0 disables rotation. Default {MONITOR_MAX_BYTES}.",
        ),
    ] = MONITOR_MAX_BYTES,
) -> None:
    """Continuously sample process and bridge health without a full capture."""
    import psutil

    if pid is None:
        pid = find_server_pid()
    if pid is None or not psutil.pid_exists(pid):
        err_console.print("[red]no unique running 7DaysToDieServe process; pass --pid[/red]")
        raise typer.Exit(2)
    process = psutil.Process(pid)
    bridge_latest = bridge_telemetry_file(pid, "apm_app_latest.json")
    taken = 0
    previous_late: int | None = None
    previous_gc: int | None = None
    # Read once, not per sample: a config re-read every interval would let an
    # edit mid-run flip-flop the stale threshold between consecutive samples.
    export_period = bridge_export_period(bridge_latest.parent)
    try:
        while count == 0 or taken < count:
            try:
                with process.oneshot():
                    cpu = process.cpu_percent(interval=interval)
                    sample: dict[str, object] = {
                        "t": time.time(),
                        "pid": pid,
                        "cpu_pct": round(cpu, 1),
                        "rss_mb": round(process.memory_info().rss / 1048576, 1),
                        "threads": process.num_threads(),
                    }
            except psutil.AccessDenied:
                # The server commonly runs as another user; a permission wall
                # must end the loop with a fix, not a bare traceback.
                err_console.print(
                    f"[red]cannot inspect pid {pid}: access denied "
                    "(process owned by another user?); run monitor as that user[/red]"
                )
                raise typer.Exit(2) from None
            if bridge_latest.is_file():
                try:
                    snapshot = json.loads(bridge_latest.read_text(encoding="utf-8"))
                    update = snapshot.get("update") or {}
                    world = snapshot.get("world") or {}
                    sample["tick_avg_ms"] = update.get("serverTickIntervalAvgMs")
                    sample["late_ticks"] = update.get("lateTicks")
                    sample["gm_update_avg_ms"] = update.get("gmUpdateDurationAvgMs")
                    sample["spikes"] = update.get("totalSpikes")
                    sample["entities"] = world.get("entities")
                    sample["players"] = world.get("clients") or world.get("players")
                    # TPS headline. serverTickIntervalAvgMs is a LIFETIME average
                    # (since apm reset), which stops reflecting current lag after
                    # hours of 24/7 uptime - prefer the instantaneous frame period
                    # from the world sample (current by construction), fall back to
                    # the lifetime average, and expose both so long-run dashboards
                    # can tell them apart. Survives when telnet is too saturated to
                    # answer, unlike a listplayers poll. Values coerce like every
                    # other unvalidated snapshot field: a string/None must read as
                    # "no data", never raise TypeError out of the sample loop.
                    frame_now = as_number(world.get("unityDeltaMs")) or 0.0
                    tick_life = as_number(update.get("serverTickIntervalAvgMs")) or 0.0
                    sample["tps"] = (
                        round(1000 / frame_now, 1)
                        if frame_now
                        else (round(1000 / tick_life, 1) if tick_life else None)
                    )
                    sample["tps_lifetime"] = round(1000 / tick_life, 1) if tick_life else None
                    # Each full (gen2) collection is a Boehm stop-the-world pause.
                    sample["full_gc"] = (snapshot.get("gc") or {}).get("gen2Collections")
                    # The bridge exports every PeriodicExportSeconds (default 30);
                    # flag samples older than that so stale reads are not mistaken
                    # for live data.
                    sample["bridge_age_s"] = round(time.time() - bridge_latest.stat().st_mtime, 1)
                except (json.JSONDecodeError, OSError):
                    pass
            current_late = sample.get("late_ticks")
            late_delta = _delta_str(current_late, previous_late, "late")
            if isinstance(current_late, int):
                previous_late = current_late
            bridge_age = sample.get("bridge_age_s")
            # Older than one and a half export periods means the exporter
            # missed at least one expected refresh: a genuinely stale read.
            # Escaped brackets: rich would otherwise parse "[bridge ...]" as an
            # unknown style tag and silently drop the warning from the console.
            stale = (
                f"  \\[bridge {bridge_age}s old]"
                if isinstance(bridge_age, float) and bridge_age > export_period * 1.5
                else ""
            )
            current_gc = sample.get("full_gc")
            gc_delta = _delta_str(current_gc, previous_gc, "fullGC", "(STW!)")
            if isinstance(current_gc, int):
                previous_gc = current_gc

            tps_str = _ms(sample.get("tps"))
            console.print(
                f"cpu={sample['cpu_pct']:6.1f}%  rss={sample['rss_mb']:8.1f}MB  "
                f"threads={sample['threads']}  tps={tps_str}  "
                f"tick={_ms(sample.get('tick_avg_ms'))}ms  gm={_ms(sample.get('gm_update_avg_ms'))}ms  "
                f"ent={sample.get('entities', '-')}  ply={sample.get('players', '-')}  "
                f"spikes={sample.get('spikes', '-')}{late_delta}{gc_delta}{stale}"
            )
            if output:
                output.parent.mkdir(parents=True, exist_ok=True)
                _rotate_monitor_log(output, max_bytes)
                with output.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(sample) + "\n")
            taken += 1
    except KeyboardInterrupt:
        # 130 = 128+SIGINT, matching scenario run's interrupt contract so
        # scripts can tell a deliberate stop from a clean end of the count.
        console.print("monitor stopped")
        raise typer.Exit(130) from None
    except psutil.NoSuchProcess:
        # Distinct from Ctrl+C: a dead target must not read as a normal stop
        # (the operator would re-check the wrong thing). Nonzero because a
        # fixed-count monitor that lost its target did not complete.
        console.print("target process exited; monitor stopped")
        raise typer.Exit(1) from None


def _ms(value: object) -> str:
    return f"{value:.1f}" if isinstance(value, (int, float)) else "-"


def _delta_str(current: object, previous: object, label: str, suffix: str = "") -> str:
    """Monotonic-counter delta since the last sample; empty until two int
    readings exist (first sample has no baseline)."""
    if not (isinstance(current, int) and isinstance(previous, int)):
        return ""
    delta = current - previous
    return f" {label}+{delta}{suffix}" if delta > 0 else f" {label}=0"


def bridge_export_period(telemetry_dir: Path) -> float:
    """Seconds between bridge exports of apm_app_latest.json.

    Read from the mod config beside the telemetry dir (default 30): the
    monitor's stale-read flag must key off the export cadence, not the sample
    interval, or every fresh-at-cadence sample is flagged stale. The upper
    bound is the bridge's own (BridgeConfig.MaxPeriodicExportSeconds), so a
    value the mod would clamp cannot leave the monitor waiting on a read that
    never comes.
    """
    config = telemetry_dir.parent / "Config" / "apmbridge.json"
    try:
        settings = load_jsonc(config)
        raw = settings.get("PeriodicExportSeconds") if isinstance(settings, dict) else None
        # bool is an int subclass: a hand-edited `true` is not a 1-second cadence.
        value = (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else DEFAULT_BRIDGE_EXPORT_SECONDS
        )
    except (OSError, ValueError, TypeError):
        return DEFAULT_BRIDGE_EXPORT_SECONDS
    if value <= 0:
        return DEFAULT_BRIDGE_EXPORT_SECONDS
    return min(value, MAX_BRIDGE_EXPORT_SECONDS)


@app.command("prune")
def prune_sessions(
    keep: Annotated[int, typer.Option(min=1, help="Number of newest sessions to retain.")] = 20,
    max_gb: Annotated[
        float | None,
        typer.Option(help="Total size budget; oldest sessions removed until under it."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option(help="List what would be deleted without deleting.")
    ] = False,
) -> None:
    """Delete old sessions beyond --keep or a total size budget."""
    max_bytes = max_gb * 1024**3 if max_gb is not None else None
    doomed = sessions_beyond_budget(list_sessions(apm_root()), keep, max_bytes)
    for old in doomed:
        console.print(("would remove " if dry_run else "removing ") + escape(str(old)))
    if dry_run:
        return
    # One shared pass retires sessions and runs both purge phases; a single
    # stuck entry must not strand the rest, so each failure reports alone.
    for _kind, entry, error in prune_store(apm_root(), doomed):
        if error is not None:
            err_console.print(
                f"[red]could not remove {escape(str(entry))}: {escape(str(error))}[/red]"
            )
    if doomed:
        # The index lists every session directory: without a refresh, a pruned
        # store keeps index.json entries (and index.html links) for evidence
        # that no longer exists.
        write_index(apm_root())
        trash = apm_root() / ".trash"
        window = (
            f"for {prune_grace_hours():g}h"
            if prune_grace_hours() > 0
            else "disabled (APM_PRUNE_GRACE_HOURS=0)"
        )
        console.print(
            f"removed sessions stay recoverable under {trash} ({window}); restore with mv"
        )


@app.command()
def compare(
    before: Annotated[Path, typer.Argument(help="Baseline finalized session.")],
    after: Annotated[Path, typer.Argument(help="Candidate finalized session.")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Report directory (default: the AFTER session)."),
    ] = None,
) -> None:
    """Diff two finalized sessions and write compare.json/compare.md."""
    for name, path in (("before", before), ("after", after)):
        if not (path / "summary.json").is_file():
            err_console.print(
                f"[red]{name} session has no summary.json in {escape(str(path))}[/red]"
            )
            raise typer.Exit(2)
    try:
        run_compare(before, after, output)
    except (OSError, ValueError) as error:
        # OSError: a session pruned between the is_file() gate above and the
        # reads inside (concurrent auto-prune) must fail like a corrupt one,
        # naming the path, never as a bare FileNotFoundError traceback.
        err_console.print(f"[red]compare failed: {escape(str(error))}[/red]")
        raise typer.Exit(1) from None


@app.command()
def budget(
    session: Annotated[Path, typer.Argument(help="Candidate finalized session to gate.")],
    budget_file: Annotated[
        Path | None, typer.Option("--budget", help="Budget JSON file (default: built-in budgets).")
    ] = None,
    baseline: Annotated[
        Path | None, typer.Option(help="Baseline session for regression deltas.")
    ] = None,
    max_regression: Annotated[
        float,
        typer.Option(
            help="Allowed increase in layer pressure points vs the baseline "
            "(absolute 0-100 scale points, not percent)."
        ),
    ] = 15,
) -> None:
    """Gate a finalized session against budgets; exits 1 on regression."""
    if not (session / "summary.json").is_file():
        err_console.print(f"[red]missing summary.json in {escape(str(session))}[/red]")
        raise typer.Exit(2)
    if budget_file is not None and not budget_file.is_file():
        err_console.print(f"[red]budget file not found: {escape(str(budget_file))}[/red]")
        raise typer.Exit(2)
    if baseline is not None and not (baseline / "summary.json").is_file():
        err_console.print(
            f"[red]baseline session has no summary.json in {escape(str(baseline))}[/red]"
        )
        raise typer.Exit(2)
    try:
        passed = check_budget(session, budget_file, baseline, max_regression)
    except (OSError, ValueError) as error:
        # OSError: a vanished/unreadable summary under a concurrent prune (or
        # an unreadable budget file) is an operator error naming the path,
        # same contract as compare above.
        err_console.print(f"[red]{escape(str(error))}[/red]")
        raise typer.Exit(2) from None
    _exit(0 if passed else 1)


@app.command()
def bridge(
    session: Annotated[Path, typer.Argument(help="Finalized session to correlate.")],
    snapshot: Annotated[
        Path | None, typer.Option(help="Optional in-game APM bridge snapshot.")
    ] = None,
) -> None:
    """Correlate managed timings into csharp_bridge.json with a remediation playbook."""
    _require_session_dir(session)
    # Same contract as compare/budget: a malformed summary.json is an operator
    # error naming the file, never a traceback (analyze re-reads unvalidated
    # session JSON; JSONDecodeError is a ValueError subclass).
    try:
        result = analyze(session, snapshot)
    except (OSError, ValueError) as error:
        # OSError: a session artifact pruned mid-analysis fails like a corrupt
        # one (same contract as compare/budget), never a bare traceback.
        err_console.print(f"[red]bridge analysis failed: {escape(str(error))}[/red]")
        raise typer.Exit(1) from None
    # Playbook lines embed managed section names and frame names taken from
    # session evidence (server-side snapshots, imported bundles); escape them so
    # bracketed names cannot render as console markup.
    console.print(escape(result["playbook_md"]))
    console.print(f"wrote {escape(str(session / 'csharp_bridge.json'))}")


@scenario_app.command("run")
def scenario_run(
    seconds: Annotated[int, typer.Option(min=1, help="Capture window duration in seconds.")] = 45,
    clients: Annotated[int, typer.Option(min=1, help="Bot clients to join.")] = 6,
    actions: Annotated[int, typer.Option(min=1, help="Actions per bot over the run.")] = 500,
    seed: Annotated[
        int, typer.Option(help="Bot action RNG seed (fixed = reproducible cohort behaviour).")
    ] = 42,
    game_port: Annotated[int, typer.Option(help="Game UDP port.")] = 26902,
    pid: Annotated[
        int | None,
        typer.Option(help="Server process ID; auto-detects the unique server when omitted."),
    ] = None,
    preset: Annotated[
        CapturePreset,
        typer.Option(
            help="Collector set: standard (app,threads,memory,cpu), deep (all), or "
            "forensic (all + mono_alloc for gross-allocation churn + STW attribution "
            "when diagnosing GC lag)"
        ),
    ] = CapturePreset.STANDARD,
    bot_mode: Annotated[
        str,
        typer.Option(
            help="One behaviour for every bot, e.g. traverse, wander, combat, bait, "
            "demolition, chatty. Passed to loadgen unvalidated; see the mode table in "
            "docs/LOAD_PROFILE.md. Use --bot-mix for a weighted cohort."
        ),
    ] = "",
    bot_mix: Annotated[
        str,
        typer.Option(
            help="Weighted per-bot mode mix, e.g. 'traverse:35,combat:20,bait:15' "
            "(heterogeneous cohort; overrides --bot-mode). See the 'canonical' profile."
        ),
    ] = "",
    spawn_entity: Annotated[str, typer.Option(help="Telnet-spawned entity class(es).")] = "",
    spawn_per_player: Annotated[
        int, typer.Option(min=0, help="Entities to spawn per joined player.")
    ] = 0,
    spawn_every_ms: Annotated[
        int, typer.Option(min=0, help="Milliseconds between spawn bursts (0 = off).")
    ] = 0,
    horde_every_ms: Annotated[
        int, typer.Option(min=0, help="Wandering-horde burst cadence (0 = off).")
    ] = 0,
    horde_waves: Annotated[int, typer.Option(min=1, help="Scout waves per horde target.")] = 3,
    max_dynamite: Annotated[
        int, typer.Option(min=0, help="Cap on concurrently live dynamite (0 = off).")
    ] = 0,
    no_spawn: Annotated[
        bool,
        typer.Option("--no-spawn/--spawn", help="Telnet zombie pressure during the capture."),
    ] = False,
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
) -> None:
    """Capture under sibling loadgen load (joins, actions, spawns)."""
    # Secret via environment only (same contract as capture): no argv flag.
    telnet_password = os.environ.get("SEVENDTD_TELNET_PASSWORD", "")
    # The matrix path calls scenario_run directly with plan-file strings, so
    # the enum coercion happens here rather than only in Typer's parser.
    try:
        chosen_preset = CapturePreset(preset)
    except ValueError:
        err_console.print("[red]preset must be one of: standard, deep, forensic[/red]")
        raise typer.Exit(2) from None
    presets = {
        CapturePreset.STANDARD: "app,threads,memory,cpu",
        CapturePreset.DEEP: "all",
        CapturePreset.FORENSIC: "all,alloc",
    }
    loadgen = REPO.parent / "7dtd-loadgen" / "scripts" / "run_loadgen.sh"
    if not loadgen.is_file():
        err_console.print(f"[red]sibling load generator not found: {escape(str(loadgen))}[/red]")
        raise typer.Exit(2)
    run_dir = apm_root() / ".scenario"
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    # Exclusive-create claim: a same-second duplicate invocation must not point
    # both loadgen runs at one manifest path.
    workload = claim_file(run_dir / f"loadgen_{stamp}.json")
    stats = workload.with_name(f"{workload.stem}_stats.json")
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
        "LOADGEN_HORDE_WAVES": str(horde_waves) if horde_waves else "",
        "LOADGEN_MAX_DYNAMITE": str(max_dynamite) if max_dynamite else "",
        "LOADGEN_NO_SPAWN": "1" if no_spawn else "",
    }
    env.update({key: value for key, value in optional_env.items() if value})
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
    console.print(
        f"starting sibling 7dtd-loadgen: clients={clients} actions={actions} "
        f"mode={bot_mode or 'auto'} warmup={warmup}s"
    )
    # Own session: the teardown below kills the whole process group, so it must
    # not share ours (and pid == pgid only with start_new_session).
    try:
        load_process = subprocess.Popen([str(loadgen)], env=env, start_new_session=True)
    except OSError as error:
        # is_file() above does not prove executability or readability: a lost
        # +x bit or unreadable interpreter must fail like every other startup
        # problem here (clean message, exit 2), not a bare traceback.
        err_console.print(
            f"[red]cannot start sibling load generator {escape(str(loadgen))}: "
            f"{escape(str(error))}[/red]"
        )
        raise typer.Exit(2) from None
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
            only=presets[chosen_preset],
            no_app=False,
            telnet_host="127.0.0.1",
            telnet_port=8081,
            telnet_password=telnet_password,
            reset_bridge=reset_bridge,
            # A scenario run is a bench capture, not a production one: the
            # cohort is synthetic, so the MAIN-thread JIT burst is safe here
            # and the managed frame attribution it buys is the point.
            symbolize=True,
            preset=str(chosen_preset),
        )
        session = outcome.session
        capture_rc = outcome.exit_code
    except RuntimeError as error:
        # Message embeds user-set hostnames/paths; escape like every other echo.
        err_console.print(f"[red]{escape(str(error))}[/red]")
        capture_rc = 2
    except KeyboardInterrupt:
        # Ctrl-C: still tear the loadgen down (finally) and report a session.
        capture_rc = 130
    finally:
        # Deterministic loadgen shutdown even when the capture is interrupted.
        # The script runs the bot client as its own child, so a plain
        # terminate()/kill() on the shell would orphan that cohort (and its
        # sockets); escalate against the whole process group instead. A second
        # Ctrl+C landing inside the grace wait falls through to the same group
        # teardown instead of skipping it and orphaning the cohort for good.
        try:
            load_rc = load_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        except KeyboardInterrupt:
            capture_rc = 130
        if load_process.poll() is None:
            reaped = terminate_tree(load_process)
            if reaped is not None:
                load_rc = reaped
    # The claim pre-creates the manifest path, so only parseable content proves
    # the loadgen actually wrote it: a torn or non-object write (loadgen killed
    # mid-flush) must not crash the attach after the capture already succeeded.
    if session is not None:
        attached = _attach_workload_manifest(session, workload, label, bot_mode)
        if attached and stats.is_file():
            # The capture already succeeded and its evidence is on disk: an
            # unreadable stats file must be reported, not raised as a traceback
            # that skips the audit below and the matrix exit code.
            try:
                shutil.copy2(stats, session / "loadgen_stats.json")
            except OSError as error:
                err_console.print(
                    f"[red]loadgen stats not attached: "
                    f"{escape(str(stats))}: {escape(str(error))}[/red]"
                )
        audit_session(session)
        if attached:
            console.print(f"workload manifest attached: {session / 'workload.json'}")
    _exit(capture_rc or load_rc)


def _attach_workload_manifest(session: Path, workload: Path, label: str, bot_mode: str) -> bool:
    """Copy the loadgen manifest into the session; True when it was attached.

    The claim pre-creates the manifest path, so only parseable content proves
    the loadgen actually wrote it: a torn, non-object, or empty write (loadgen
    killed mid-flush) must not crash the attach after the capture succeeded.
    """
    if workload.stat().st_size == 0:
        return False
    try:
        doc = json.loads(workload.read_text(encoding="utf-8"))
    except ValueError as error:
        err_console.print(
            f"[red]loadgen manifest unreadable, not attached: "
            f"{escape(str(workload))}: {escape(str(error))}[/red]"
        )
        return False
    if not isinstance(doc, dict):
        err_console.print(
            f"[red]loadgen manifest is not a JSON object, not attached: "
            f"{escape(str(workload))}[/red]"
        )
        return False
    if label:
        doc["label"] = label
    doc.setdefault("workload", {})["botMode"] = bot_mode or doc.get("workload", {}).get(
        "botMode", "auto"
    )
    atomic_json(session / "workload.json", doc)
    return True


# Plan entries reach scenario_run as a direct Python call, bypassing Typer's
# option parsing, so their values are checked here against the same types the
# CLI annotations declare. A mistyped value (e.g. "seconds": "60") would
# otherwise raise TypeError deep inside run_capture - after that experiment's
# loadgen started and any earlier experiments had already run.
_MATRIX_ENTRY_TYPES: dict[str, type] = {
    "seconds": int,
    "clients": int,
    "actions": int,
    "seed": int,
    "spawn_per_player": int,
    "spawn_every_ms": int,
    "horde_every_ms": int,
    "horde_waves": int,
    "max_dynamite": int,
    "warmup": int,
    "no_spawn": bool,
    "rally": bool,
    "reset_bridge": bool,
    "preset": str,
    "bot_mode": str,
    "bot_mix": str,
    "spawn_entity": str,
    "rally_at": str,
    "label": str,
}


def _coerce_matrix_entry(entry: dict[str, object], position: int) -> dict[str, Any]:
    """Type-check one plan entry before any side effect; returns it unchanged
    when every value already matches its declared type."""
    for key, value in entry.items():
        expected = _MATRIX_ENTRY_TYPES.get(key)
        if expected is None:
            continue  # unknown keys are rejected by the caller
        if expected is bool:
            valid = isinstance(value, bool)
        elif expected is int:
            # JSON has no integer type: a whole-number float ("seconds": 60.0)
            # counts as an int; bool is an int subclass but never a count.
            if isinstance(value, bool):
                valid = False
            elif isinstance(value, int):
                valid = True
            else:
                valid = isinstance(value, float) and value.is_integer()
        else:
            valid = isinstance(value, str)
        if not valid:
            err_console.print(
                f"[red]plan entry {position} field '{key}': expected "
                f"{expected.__name__}, got {value!r}[/red]"
            )
            raise typer.Exit(2)
    return entry


@scenario_app.command("matrix")
def scenario_matrix(
    plan: Annotated[Path, typer.Argument(help="JSON plan: a list of experiment objects.")],
    game_port: Annotated[int, typer.Option(help="Game UDP port.")] = 26902,
    cleanup: Annotated[
        str, typer.Option(help="Console command run between experiments ('' disables).")
    ] = "killall",
) -> None:
    """Run a labeled experiment sequence from a JSON plan (list of scenario kwargs)."""
    from .capture import telnet_command

    # Secret via environment only (same contract as capture): no argv flag.
    telnet_password = os.environ.get("SEVENDTD_TELNET_PASSWORD", "")
    if not plan.is_file():
        err_console.print(f"[red]plan file not found: {escape(str(plan))}[/red]")
        raise typer.Exit(2)
    try:
        entries = json.loads(plan.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise typer.BadParameter(f"plan is not valid JSON ({plan}): {error}") from None
    if not isinstance(entries, list) or not entries:
        err_console.print("[red]plan must be a non-empty JSON list of experiment objects[/red]")
        raise typer.Exit(2)
    allowed = set(_MATRIX_ENTRY_TYPES)
    results: list[tuple[str, int]] = []
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            err_console.print(f"[red]plan entry {position} is not a JSON object[/red]")
            raise typer.Exit(2)
        # `_`-prefixed keys are plan commentary (the shipped plans document each
        # experiment with one) and carry no runner meaning.
        unknown = {key for key in set(entry) - allowed if not key.startswith("_")}
        if unknown:
            # Plan keys are attacker-controlled in imported plans; escape them.
            err_console.print(
                f"[red]plan entry {position} has unknown keys: {escape(str(sorted(unknown)))}[/red]"
            )
            raise typer.Exit(2)
        # Fail before the cleanup telnet round-trip: a mistyped entry must not
        # run the previous experiment's world-wiping console command for
        # nothing (and must never reach the loadgen with junk values).
        kwargs = _coerce_matrix_entry(entry, position)
        label = str(entry.get("label") or f"experiment-{position}")
        if cleanup:
            # A failed cleanup must be visible: leftover entities from one
            # experiment silently inflate the next one's measurements.
            if not telnet_command("127.0.0.1", 8081, telnet_password, cleanup):
                err_console.print(
                    f"[yellow]cleanup '{escape(cleanup)}' failed (telnet); "
                    "leftover entities may contaminate the next experiment[/yellow]"
                )
            time.sleep(8)
        console.print(f"[bold]=== matrix {position}/{len(entries)}: {escape(label)}[/bold]")
        code = 0
        try:
            scenario_run(
                game_port=game_port,
                **{**kwargs, "label": label},
            )
        except typer.Exit as stop:
            code = stop.exit_code or 0
        results.append((label, code))
    for label, code in results:
        console.print(f"  {escape(label)}: exit={code}")
    _exit(0 if all(code in (0, 1) for _, code in results) else 1)


@flame_app.command("build")
def flame_build(
    directory: Annotated[Path, typer.Argument(help="Session directory with captured stacks.")],
) -> None:
    """Render flamegraphs from a session's captured stacks."""
    _require_backends()
    _exit(run([str(REPO / "tools/host_profiler/make_flames.sh"), str(directory)]))


@flame_app.command("diff")
def flame_diff(
    before: Annotated[Path, typer.Argument(help="Baseline session directory.")],
    after: Annotated[Path, typer.Argument(help="Candidate session directory.")],
) -> None:
    """Build a differential flamegraph HTML from two sessions."""
    _require_backends()
    _exit(
        backend_python(REPO / "tools/host_profiler/flame_diff_html.py", [str(before), str(after)])
    )


if __name__ == "__main__":
    app()
