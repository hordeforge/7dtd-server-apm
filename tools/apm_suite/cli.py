from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .analysis.bridge import analyze
from .analysis.budget import check_budget
from .analysis.compare import run_compare
from .analysis.index import write_index
from .capture import find_server_pid, run_capture, unknown_only_tokens, write_plan_text
from .doctor import inspect
from .finalize import finalize as finalize_session
from .io import atomic_json, atomic_text, claim_dir, claim_file, load_json
from .models import as_number
from .paths import REPO, apm_root, require_backends
from .runner import backend_python, run, terminate_tree
from .session import (
    audit_session,
    list_sessions,
    prune_grace_hours,
    purge_expired_trash,
    remove_sessions,
    sessions_beyond_budget,
)

app = typer.Typer(help="Host-only APM for 7 Days to Die dedicated servers.", no_args_is_help=True)
flame_app = typer.Typer(help="Build and compare flame profiles.", no_args_is_help=True)
scenario_app = typer.Typer(
    help="Run an APM capture under sibling load generation.", no_args_is_help=True
)
app.add_typer(flame_app, name="flame")
app.add_typer(scenario_app, name="scenario")
console = Console()
err_console = Console(stderr=True)


def _exit(code: int) -> None:
    if code:
        raise typer.Exit(code)


def _require_backends() -> None:
    """CLI boundary for paths.require_backends: clean error instead of a traceback."""
    try:
        require_backends()
    except RuntimeError as error:
        err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(2) from None


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
    """Host-only APM for 7 Days to Die dedicated servers."""


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
    result = inspect(pid, telnet_host, telnet_port)
    if json_output == Path("-"):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
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
    seconds: Annotated[int, typer.Option(min=1, help="Capture duration in seconds.")] = 45,
    pid: Annotated[
        int | None,
        typer.Option(help="Server process ID; auto-detects the unique server when omitted."),
    ] = None,
    only: Annotated[str, typer.Option(help="Comma-separated collector names or layers.")] = "all",
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
            help="Export the full JIT map so managed frames resolve to method names. "
            "The map burst runs on the server's MAIN thread and can freeze a loaded "
            "server for tens of seconds - default OFF so a capture against a "
            "production server is safe; pass --symbolize for bench/flamegraph runs."
        ),
    ] = False,
    dry_run: Annotated[bool, typer.Option(help="Print the resolved collector plan.")] = False,
) -> None:
    """Run a timed collector session against the server and finalize it."""
    if unknown := unknown_only_tokens(only):
        err_console.print(
            f"[red]unknown --only value(s): {', '.join(unknown)}; "
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
        err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(2) from None
    console.print(f"APM session: {outcome.session}")
    _exit(outcome.exit_code)


@app.command()
def finalize(
    session: Path,
    skip_bridge: Annotated[
        bool, typer.Option(help="Skip the managed bridge correlation stage.")
    ] = False,
) -> None:
    """Run finalization stages and write summary artifacts for a raw session."""
    if not session.is_dir():
        err_console.print(f"[red]not a session directory: {session}[/red]")
        raise typer.Exit(2)
    _exit(finalize_session(session, skip_bridge=skip_bridge).exit_code)


@app.command()
def audit(
    session: Path,
    strict: Annotated[
        bool, typer.Option(help="Exit 1 when warnings are present too, not only errors.")
    ] = False,
) -> None:
    """Verify session artifact integrity against recorded hashes."""
    if not session.is_dir():
        err_console.print(f"[red]not a session directory: {session}[/red]")
        raise typer.Exit(2)
    manifest, valid = audit_session(session, verify_recorded=True)
    console.print(
        f"audit: {'valid' if valid else 'INVALID'}; "
        f"{len(manifest.errors)} errors, {len(manifest.warnings)} warnings"
    )
    # An INVALID verdict without the offending paths is not actionable; name
    # every error (missing file, failed schema, recorded-hash mismatch).
    for error in manifest.errors:
        err_console.print(f"[red]{error}[/red]")
    _exit(1 if not valid or (strict and manifest.warnings) else 0)


@app.command()
def index(
    root: Annotated[
        Path | None, typer.Option(help="Sessions directory (default: the APM data root).")
    ] = None,
) -> None:
    """Write the HTML session index over all finalized sessions."""
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


def _scrub_jsonl(text: str, home: str) -> str:
    """Apply the JSON scrub per line; a malformed trailing line keeps its content
    but still loses the host home prefix."""
    out: list[str] = []
    for line in text.splitlines():
        try:
            out.append(json.dumps(_scrub(json.loads(line))).replace(home, "~"))
        except json.JSONDecodeError:
            out.append(line.replace(home, "~"))
    return "".join(line + "\n" for line in out)


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
            # perf.script / report txt / stacks.folded / flame.html / bpftrace
            # *.out / flame.svg embed dso or file paths like /home/<user>/... -
            # replace the home prefix so bundles do not leak the host username.
            # Applied to known-text artifacts only.
            home = str(Path.home())
            text_suffixes = {".txt", ".folded", ".html", ".script", ".md", ".log", ".out", ".svg"}
            # Sorted walk: identical session content must yield an identical
            # member order, not a readdir-order zip layout.
            for source in sorted(session.rglob("*")):
                if not source.is_file() or source.name in excluded or source.suffix == ".err":
                    continue
                relative = source.relative_to(session)
                if source.suffix == ".json":
                    try:
                        data = json.loads(source.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError) as error:
                        raise typer.BadParameter(f"cannot parse {relative}: {error}") from None
                    sanitized = temp / relative
                    sanitized.parent.mkdir(parents=True, exist_ok=True)
                    sanitized.write_text(
                        json.dumps(_scrub(data), indent=2).replace(home, "~") + "\n",
                        encoding="utf-8",
                    )
                    archive.write(sanitized, relative)
                elif source.suffix == ".jsonl":
                    try:
                        text = source.read_text(errors="replace", encoding="utf-8")
                    except OSError:
                        archive.write(source, relative)
                        continue
                    sanitized = temp / relative
                    sanitized.parent.mkdir(parents=True, exist_ok=True)
                    sanitized.write_text(_scrub_jsonl(text, home), encoding="utf-8")
                    archive.write(sanitized, relative)
                elif source.suffix in text_suffixes:
                    try:
                        text = source.read_text(errors="replace", encoding="utf-8")
                    except OSError:
                        archive.write(source, relative)
                        continue
                    sanitized = temp / relative
                    sanitized.parent.mkdir(parents=True, exist_ok=True)
                    sanitized.write_text(text.replace(home, "~"), encoding="utf-8")
                    archive.write(sanitized, relative)
                else:
                    archive.write(source, relative)
        os.replace(tmp_zip_path, output)
    finally:
        tmp_zip_path.unlink(missing_ok=True)
    console.print(f"sanitized bundle: {output}")


def _member_is_safe(name: str) -> bool:
    # Zip-slip guard: bundle members must stay inside the restore target.
    candidate = PurePosixPath(name)
    return not candidate.is_absolute() and ".." not in candidate.parts


# Decompression-bomb guard for imported evidence bundles (they arrive from
# other people): CPython's extractall caps each member at its declared
# file_size, so the sum of declared sizes is a reliable upper bound on what
# lands on disk. Sessions are tens of MB; 2 GiB total / 20k members is far
# above any legitimate bundle while stopping a small archive from filling
# the session store volume.
MAX_IMPORT_MEMBERS = 20_000
MAX_IMPORT_UNCOMPRESSED_BYTES = 2 * 1024**3


@app.command("import")
def import_bundle(
    bundle: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    store: Annotated[
        Path | None,
        typer.Option("--store", help="Session store root (default: the APM session store)."),
    ] = None,
) -> None:
    """Restore an exported support bundle into the session store and audit it."""
    # NFC at ingestion: a macOS NFD filename and its NFC spelling must claim
    # the same session directory name, or later lookups by the typed form miss.
    stem = "".join(
        c if c.isalnum() or c in "._-" else "_" for c in unicodedata.normalize("NFC", bundle.stem)
    ).strip("._")
    if not stem.startswith("session_"):
        stem = f"session_{stem}"
    try:
        with zipfile.ZipFile(bundle) as archive:
            infos = archive.infolist()
            declared_bytes = sum(info.file_size for info in infos)
            if len(infos) > MAX_IMPORT_MEMBERS or declared_bytes > MAX_IMPORT_UNCOMPRESSED_BYTES:
                err_console.print(
                    f"[red]refusing bundle beyond import limits "
                    f"({len(infos)} members, {declared_bytes} uncompressed bytes; "
                    f"max {MAX_IMPORT_MEMBERS}/{MAX_IMPORT_UNCOMPRESSED_BYTES})[/red]"
                )
                raise typer.Exit(2)
            unsafe = [m for m in archive.namelist() if not _member_is_safe(m)]
            if unsafe:
                err_console.print(
                    f"[red]refusing bundle with unsafe member path(s): {', '.join(unsafe)}[/red]"
                )
                raise typer.Exit(2)
            # Exclusive-create claim, made only after the bundle is proven safe,
            # so a rejected import never litters the store with an empty session
            # dir; a concurrent duplicate import of the same bundle gets its own
            # target instead of merging into this one mid-extract.
            target = claim_dir((store or apm_root()) / stem)
            archive.extractall(target)
    except zipfile.BadZipFile as error:
        err_console.print(f"[red]{bundle} is not a readable zip bundle: {error}[/red]")
        raise typer.Exit(2) from None
    manifest, valid = audit_session(target)
    outcome = (
        "audit passed"
        if valid
        else f"{len(manifest.errors)} error(s), {len(manifest.warnings)} warning(s)"
    )
    console.print(f"restored {target} ({outcome})")


@app.command("scaling")
def scaling(
    sessions: Annotated[list[Path], typer.Argument(help="Finalized sessions from a scale ladder.")],
    by: Annotated[str, typer.Option(help="Scale variable: players or entities.")] = "players",
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Rank managed sections by load-scaling exponent across a session ladder.

    Fits each section's cost vs load (players or entities) on a log-log fit and
    ranks by exponent (O(N^exp), worst first). Needs 3+ finalized sessions at
    distinct load levels.
    """
    from .analysis.scaling import analyze_scaling

    if by not in {"players", "entities"}:
        err_console.print(f"[red]--by must be 'players' or 'entities', got {by!r}[/red]")
        raise typer.Exit(2)
    usable = [s for s in sessions if (s / "summary.json").is_file()]
    if len(usable) < 3:
        err_console.print(
            "[red]need >= 3 finalized sessions (different load levels) to fit scaling[/red]"
        )
        raise typer.Exit(2)
    result = analyze_scaling(usable, scale_key=by)
    distinct = sorted(set(result["scales"]))
    if len(distinct) < 3:
        err_console.print(
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
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise typer.BadParameter(f"unreadable {summary_path}: {error}") from None
    lines = [
        "# HELP sevendtd_apm_layer_pressure Layer pressure from a collected APM layer.",
        "# TYPE sevendtd_apm_layer_pressure gauge",
    ]
    for layer in summary.get("layers") or []:
        # summary.json is re-read without schema guarantees (hand-edited or
        # imported), so every numeric field goes through a safe coercion: a
        # crafted value must degrade to "no line", never raise mid-export.
        score = as_number(layer.get("score"))
        if layer.get("state") == "collected" and score is not None:
            name = _prom_label(layer.get("layer", "unknown"))
            lines.append(f'sevendtd_apm_layer_pressure{{layer="{name}"}} {score:.6f}')
    health_path = session / "health.json"
    health = load_json(health_path) if health_path.is_file() else summary.get("health") or {}
    coverage = as_number(health.get("coverage"))
    if coverage is not None:
        lines += [
            "# TYPE sevendtd_apm_coverage gauge",
            f"sevendtd_apm_coverage {coverage:.6f}",
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
                scaled = as_number(entry.get("scaled_total_ms"))
                if subsystem is None or scaled is None:
                    continue
                name = _prom_label(subsystem)
                lines.append(f'sevendtd_apm_subsystem_ms{{subsystem="{name}"}} {scaled:.3f}')
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
                severity = as_number(cause.get("severity")) or 0.0
                lines.append(f'sevendtd_apm_lag_cause_severity{{cause="{name}"}} {severity:.3f}')
    frame = (summary.get("metadata") or {}).get("frame") or {}
    late_ticks = as_number(frame.get("lateTicks"))
    if late_ticks is not None:
        lines += [
            "# TYPE sevendtd_apm_late_ticks gauge",
            f"sevendtd_apm_late_ticks {int(late_ticks)}",
        ]
    gc_meta = (summary.get("metadata") or {}).get("gc") or {}
    alloc_rate = as_number(gc_meta.get("allocMBPerSecond"))
    if alloc_rate is not None:
        lines += [
            "# TYPE sevendtd_apm_alloc_mb_per_second gauge",
            f"sevendtd_apm_alloc_mb_per_second {alloc_rate:.3f}",
        ]
    gross_rate = as_number(gc_meta.get("grossAllocMBPerSecond"))
    if gross_rate is not None:
        lines += [
            "# TYPE sevendtd_apm_gross_alloc_mb_per_second gauge",
            f"sevendtd_apm_gross_alloc_mb_per_second {gross_rate:.3f}",
        ]
    gc_layer = next(
        (
            layer.get("signals") or {}
            for layer in (summary.get("layers") or [])
            if layer.get("layer") == "runtime_gc"
        ),
        {},
    )
    stw_worst = as_number(gc_layer.get("stw_pause_worst_ms"))
    if stw_worst is not None:
        stw_total = as_number(gc_layer.get("stw_pause_total_ms")) or 0.0
        lines += [
            "# TYPE sevendtd_apm_gc_stw_worst_ms gauge",
            f"sevendtd_apm_gc_stw_worst_ms {stw_worst:.3f}",
            "# TYPE sevendtd_apm_gc_stw_total_ms gauge",
            f"sevendtd_apm_gc_stw_total_ms {stw_total:.3f}",
        ]
    # Kernel UDP send is the honest windowed chunk rate (bridge transfers is a
    # join-burst-weighted lifetime average; see report R56).
    net_meta = (summary.get("metadata") or {}).get("net") or {}
    udp_send = as_number(net_meta.get("udp_send_mb_per_second"))
    if udp_send is not None:
        lines += [
            "# TYPE sevendtd_apm_udp_send_mb_per_second gauge",
            f"sevendtd_apm_udp_send_mb_per_second {udp_send:.3f}",
        ]
    # Atomic write: a scrape racing the export must not read a truncated file.
    atomic_text(output, "\n".join(lines) + "\n")
    console.print(f"Prometheus metrics: {output}")


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
            help="Append JSONL here. Grows without bound - if run as a 24/7 service, "
            "rotate it (logrotate or a size check); ~1 line per interval.",
        ),
    ] = None,
) -> None:
    """Continuously sample process and bridge health without a full capture."""
    import psutil

    if pid is None:
        pid = find_server_pid()
    if pid is None or not psutil.pid_exists(pid):
        err_console.print("[red]no unique running 7DaysToDieServe process; pass --pid[/red]")
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
                    # answer, unlike a listplayers poll.
                    frame_now = world.get("unityDeltaMs") or 0
                    tick_life = update.get("serverTickIntervalAvgMs") or 0
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
            late_delta = ""
            if isinstance(current_late, int) and isinstance(previous_late, int):
                delta = current_late - previous_late
                late_delta = f" late+{delta}" if delta > 0 else " late=0"
            if isinstance(current_late, int):
                previous_late = current_late
            bridge_age = sample.get("bridge_age_s")
            # Older than one and a half export periods means the exporter
            # missed at least one expected refresh: a genuinely stale read.
            export_period = bridge_export_period(bridge_latest.parent)
            stale = (
                f"  [bridge {bridge_age}s old]"
                if isinstance(bridge_age, float) and bridge_age > export_period * 1.5
                else ""
            )
            current_gc = sample.get("full_gc")
            gc_delta = ""
            if isinstance(current_gc, int) and isinstance(previous_gc, int):
                d = current_gc - previous_gc
                gc_delta = f" fullGC+{d}(STW!)" if d > 0 else " fullGC=0"
            if isinstance(current_gc, int):
                previous_gc = current_gc

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
                with output.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(sample) + "\n")
            taken += 1
    except (KeyboardInterrupt, psutil.NoSuchProcess):
        console.print("monitor stopped")


def _ms(value: object) -> str:
    return f"{value:.1f}" if isinstance(value, (int, float)) else "-"


def bridge_export_period(telemetry_dir: Path) -> float:
    """Seconds between bridge exports of apm_app_latest.json.

    Read from the mod config beside the telemetry dir (default 30): the
    monitor's stale-read flag must key off the export cadence, not the sample
    interval, or every fresh-at-cadence sample is flagged stale.
    """
    config = telemetry_dir.parent / "Config" / "apmbridge.json"
    try:
        value = float(json.loads(config.read_text(encoding="utf-8")).get("PeriodicExportSeconds"))
    except (OSError, ValueError, TypeError):
        return 30.0
    return value if value > 0 else 30.0


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
        console.print(("would remove " if dry_run else "removing ") + str(old))
    if dry_run:
        return
    grace = prune_grace_hours()
    for old, error in remove_sessions(doomed, grace):
        # One stuck session must not strand the rest: report and keep going.
        if error is not None:
            err_console.print(f"[red]could not remove {old}: {error}[/red]")
    for entry, error in purge_expired_trash(apm_root(), grace):
        if error is not None:
            err_console.print(f"[red]could not purge {entry}: {error}[/red]")
    if doomed:
        trash = apm_root() / ".trash"
        window = f"for {grace:g}h" if grace > 0 else "disabled (APM_PRUNE_GRACE_HOURS=0)"
        console.print(
            f"removed sessions stay recoverable under {trash} ({window}); restore with mv"
        )


@app.command()
def compare(
    before: Path,
    after: Path,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Report directory (default: the AFTER session)."),
    ] = None,
) -> None:
    """Diff two finalized sessions and write compare.json/compare.md."""
    for name, path in (("before", before), ("after", after)):
        if not (path / "summary.json").is_file():
            err_console.print(f"[red]{name} session has no summary.json in {path}[/red]")
            raise typer.Exit(2)
    try:
        run_compare(before, after, output)
    except ValueError as error:
        err_console.print(f"[red]compare failed: {error}[/red]")
        raise typer.Exit(1) from None


@app.command()
def budget(
    session: Path,
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
        err_console.print(f"[red]missing summary.json in {session}[/red]")
        raise typer.Exit(2)
    if budget_file is not None and not budget_file.is_file():
        err_console.print(f"[red]budget file not found: {budget_file}[/red]")
        raise typer.Exit(2)
    if baseline is not None and not (baseline / "summary.json").is_file():
        err_console.print(f"[red]baseline session has no summary.json in {baseline}[/red]")
        raise typer.Exit(2)
    _exit(0 if check_budget(session, budget_file, baseline, max_regression) else 1)


@app.command()
def bridge(
    session: Path,
    snapshot: Annotated[
        Path | None, typer.Option(help="Optional in-game APM bridge snapshot.")
    ] = None,
) -> None:
    """Correlate managed timings into csharp_bridge.json with a remediation playbook."""
    if not session.is_dir():
        err_console.print(f"[red]not a session directory: {session}[/red]")
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
    game_port: Annotated[int, typer.Option(help="Game UDP port.")] = 26902,
    pid: Annotated[
        int | None,
        typer.Option(help="Server process ID; auto-detects the unique server when omitted."),
    ] = None,
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
    telnet_password: Annotated[
        str,
        typer.Option(envvar="SEVENDTD_TELNET_PASSWORD", help="Prefer the environment variable."),
    ] = "",
) -> None:
    """Capture under sibling loadgen load (joins, actions, spawns)."""
    presets = {"standard": "app,threads,memory,cpu", "deep": "all", "forensic": "all,alloc"}
    if preset not in presets:
        err_console.print("[red]preset must be standard, deep, or forensic[/red]")
        raise typer.Exit(2)
    loadgen = REPO.parent / "7dtd-loadgen" / "scripts" / "run_loadgen.sh"
    if not loadgen.is_file():
        err_console.print(f"[red]sibling load generator not found: {loadgen}[/red]")
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
        "LOADGEN_HORDE_WAVES": str(horde_waves) if horde_every_ms else "",
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
    load_process = subprocess.Popen([str(loadgen)], env=env, start_new_session=True)
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
        err_console.print(f"[red]{error}[/red]")
        capture_rc = 2
    except KeyboardInterrupt:
        # Ctrl-C: still tear the loadgen down (finally) and report a session.
        capture_rc = 130
    finally:
        # Deterministic loadgen shutdown even when the capture is interrupted.
        # The script runs the bot client as its own child, so a plain
        # terminate()/kill() on the shell would orphan that cohort (and its
        # sockets); escalate against the whole process group instead.
        try:
            load_rc = load_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            reaped = terminate_tree(load_process)
            if reaped is not None:
                load_rc = reaped
    # The claim pre-creates the manifest path, so only content proves the
    # loadgen actually wrote it (an empty marker must not crash the attach).
    if session is not None and workload.stat().st_size > 0:
        doc = json.loads(workload.read_text(encoding="utf-8"))
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
    game_port: Annotated[int, typer.Option(help="Game UDP port.")] = 26902,
    cleanup: Annotated[
        str, typer.Option(help="Console command run between experiments ('' disables).")
    ] = "killall",
    telnet_password: Annotated[
        str,
        typer.Option(envvar="SEVENDTD_TELNET_PASSWORD", help="Prefer the environment variable."),
    ] = "",
) -> None:
    """Run a labeled experiment sequence from a JSON plan (list of scenario kwargs)."""
    from .capture import telnet_command

    if not plan.is_file():
        err_console.print(f"[red]plan file not found: {plan}[/red]")
        raise typer.Exit(2)
    try:
        entries = json.loads(plan.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise typer.BadParameter(f"plan is not valid JSON ({plan}): {error}") from None
    if not isinstance(entries, list) or not entries:
        err_console.print("[red]plan must be a non-empty JSON list of experiment objects[/red]")
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
            err_console.print(
                f"[red]plan entry {position} has unknown keys: {sorted(unknown)}[/red]"
            )
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
    """Render flamegraphs from a session's captured stacks."""
    _require_backends()
    _exit(run([str(REPO / "tools/host_profiler/make_flames.sh"), str(directory)], check=False))


@flame_app.command("diff")
def flame_diff(before: Path, after: Path) -> None:
    """Build a differential flamegraph HTML from two sessions."""
    _require_backends()
    _exit(
        backend_python(REPO / "tools/host_profiler/flame_diff_html.py", [str(before), str(after)])
    )


if __name__ == "__main__":
    app()
