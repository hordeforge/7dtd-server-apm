"""Typed capture orchestration: collector adapters, structured results, finalize.

Replaces the former capture.sh orchestration. Every collector (python sampler,
perf wrapper, raw bpftrace probe) runs behind a CollectorSpec adapter and emits
a versioned CollectorResult JSON next to its primary artifact.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import ValidationError

from . import __version__
from .io import atomic_json, claim_dir
from .models import BridgeSnapshotV3, CollectorResult, MetaV2, schema_dict
from .paths import APM_BACKENDS, TOOLS, apm_root, require_backends
from .session import (
    keep_sessions_budget,
    list_sessions,
    prune_grace_hours,
    purge_expired_trash,
    remove_sessions,
    sessions_beyond_budget,
)

EBPF = TOOLS / "host_profiler"
# Single source of truth for the analyzer version is the package version
# (pyproject.toml / __init__.py); compare gates session compatibility on it.
ANALYZER_VERSION = __version__
# minimum extra seconds beyond the capture window for perf post-processing
# (flame build); long captures get a full extra window since perf script/report
# time grows with recorded data volume
GRACE_SECONDS = 60


@dataclass
class CaptureContext:
    session: Path
    pid: int
    comm: str
    seconds: int
    telnet_host: str = "127.0.0.1"
    telnet_port: int = 8081
    telnet_password: str = ""
    mono_so: Path | None = None
    sudo_ok: bool = False


@dataclass(frozen=True)
class CollectorSpec:
    name: str
    layer: str
    artifact: str  # session-relative primary artifact
    tool: str  # binary whose version is recorded
    aliases: frozenset[str]
    build: Callable[[CaptureContext], list[str] | None]  # None => unavailable
    needs_sudo: bool = False
    env: Callable[[CaptureContext], dict[str, str]] | None = None
    stdout_to: str | None = None  # session-relative stdout tee target
    optin: bool = False  # skipped by "all"; runs only when named/aliased explicitly


def _bt(spec_name: str, script: Path) -> Callable[[CaptureContext], list[str] | None]:
    def build(ctx: CaptureContext) -> list[str] | None:
        prepared = ctx.session / "bt" / f"{spec_name}.bt"
        cmd = [
            sys.executable,
            str(EBPF / "preprocess_bt.py"),
            str(script),
            "-o",
            str(prepared),
            "--pid",
            str(ctx.pid),
            "--comm",
            ctx.comm,
            "--mono-so",
            str(ctx.mono_so or ""),
        ]
        try:
            if subprocess.run(cmd, check=False, timeout=30).returncode:
                return None
        except (subprocess.TimeoutExpired, OSError):
            return None  # a hung preprocessor must not block the collector pipeline
        return [
            "sudo",
            "-n",
            "timeout",
            "--signal=INT",
            f"{ctx.seconds}s",
            "bpftrace",
            str(prepared),
        ]

    return build


def _mono_gc(ctx: CaptureContext) -> list[str] | None:
    if ctx.mono_so is None:
        return None
    return _bt("mono_gc", EBPF / "scripts/mono_gc.bt")(ctx)


def _mono_alloc(ctx: CaptureContext) -> list[str] | None:
    if ctx.mono_so is None:
        return None
    return _bt("mono_alloc", APM_BACKENDS / "collectors/mono_alloc.bt")(ctx)


SPECS: tuple[CollectorSpec, ...] = (
    CollectorSpec(
        name="app",
        layer="app_sim",
        artifact="app/bridge.jsonl",
        tool="python3",
        aliases=frozenset({"app"}),
        build=lambda ctx: [
            sys.executable,
            str(APM_BACKENDS / "collectors/app_scrape.py"),
            "--host",
            ctx.telnet_host,
            "--port",
            str(ctx.telnet_port),
            "--seconds",
            str(ctx.seconds),
            "--interval",
            str(ctx.seconds // 3 if ctx.seconds > 20 else 10),
            "--out",
            str(ctx.session / "app/bridge.jsonl"),
        ],
        env=lambda ctx: {"SEVENDTD_TELNET_PASSWORD": ctx.telnet_password},
    ),
    CollectorSpec(
        name="threads",
        layer="threads",
        artifact="threads/threads.jsonl",
        tool="python3",
        aliases=frozenset({"threads"}),
        build=lambda ctx: [
            sys.executable,
            str(APM_BACKENDS / "collectors/threads.py"),
            "--pid",
            str(ctx.pid),
            "--seconds",
            str(ctx.seconds),
            "--interval",
            "1",
            "--jsonl",
            str(ctx.session / "threads/threads.jsonl"),
        ],
        stdout_to="threads/threads.txt",
    ),
    CollectorSpec(
        name="proc",
        layer="memory_cache",
        artifact="memory/proc.jsonl",
        tool="python3",
        aliases=frozenset({"threads", "memory", "hw", "cache"}),
        build=lambda ctx: [
            sys.executable,
            str(EBPF / "proc_sample.py"),
            "--pid",
            str(ctx.pid),
            "--seconds",
            str(ctx.seconds),
            "--interval",
            "1",
            "--json",
            str(ctx.session / "memory/proc.jsonl"),
            "--threads",
        ],
        stdout_to="memory/proc.txt",
    ),
    CollectorSpec(
        name="hw",
        layer="memory_cache",
        artifact="memory/hw_stat.txt",
        tool="perf",
        aliases=frozenset({"memory", "hw", "cache"}),
        build=lambda ctx: [
            "bash",
            str(APM_BACKENDS / "collectors/hw_perf.sh"),
            str(ctx.pid),
            str(ctx.seconds),
            str(ctx.session / "memory"),
        ],
    ),
    CollectorSpec(
        name="perf",
        layer="cpu",
        artifact="cpu/perf/stacks.folded",
        tool="perf",
        aliases=frozenset({"cpu"}),
        build=lambda ctx: [
            "bash",
            str(EBPF / "perf_record.sh"),
            str(ctx.session / "cpu/perf"),
            str(ctx.seconds),
            str(ctx.pid),
        ],
        stdout_to="cpu/perf_launcher.log",
    ),
    CollectorSpec(
        name="oncpu",
        layer="cpu",
        artifact="cpu/oncpu.bt.out",
        tool="bpftrace",
        aliases=frozenset({"cpu"}),
        build=_bt("oncpu", EBPF / "scripts/cpu_profile.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="runqlat",
        layer="scheduler",
        artifact="scheduler/runqlat.bt.out",
        tool="bpftrace",
        aliases=frozenset({"scheduler", "sched"}),
        build=_bt("runqlat", EBPF / "scripts/runqlat.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="offcpu",
        layer="scheduler",
        artifact="scheduler/offcpu.bt.out",
        tool="bpftrace",
        aliases=frozenset({"scheduler", "sched"}),
        build=_bt("offcpu", EBPF / "scripts/offcpu.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="states",
        layer="scheduler",
        artifact="scheduler/states.bt.out",
        tool="bpftrace",
        aliases=frozenset({"scheduler", "sched"}),
        build=_bt("states", APM_BACKENDS / "collectors/sched_states.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="futex",
        layer="sync_locks",
        artifact="sync/futex.bt.out",
        tool="bpftrace",
        aliases=frozenset({"sync", "locks", "futex"}),
        build=_bt("futex", APM_BACKENDS / "collectors/futex.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="vfs",
        layer="io",
        artifact="io/vfs.bt.out",
        tool="bpftrace",
        aliases=frozenset({"io"}),
        build=_bt("vfs", APM_BACKENDS / "collectors/vfs_lat.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="block",
        layer="io",
        artifact="io/block.bt.out",
        tool="bpftrace",
        aliases=frozenset({"io"}),
        build=_bt("block", APM_BACKENDS / "collectors/block_lat.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="io_net",
        layer="io",
        artifact="io/io_net.bt.out",
        tool="bpftrace",
        aliases=frozenset({"io", "net"}),
        build=_bt("io_net", EBPF / "scripts/io_net.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="mono_gc",
        layer="runtime_gc",
        artifact="runtime/mono_gc.bt.out",
        tool="bpftrace",
        aliases=frozenset({"runtime", "gc"}),
        build=_mono_gc,
        needs_sudo=True,
    ),
    CollectorSpec(
        name="mono_alloc",
        layer="runtime_gc",
        artifact="runtime/mono_alloc.bt.out",
        tool="bpftrace",
        aliases=frozenset({"alloc", "allocsites"}),
        build=_mono_alloc,
        needs_sudo=True,
        optin=True,  # high overhead: opt in with --only alloc (forensic)
    ),
)


def wanted(spec: CollectorSpec, only: str) -> bool:
    requested = {token.strip() for token in only.split(",") if token.strip()}
    explicit = bool({spec.name, spec.layer} & requested or spec.aliases & requested)
    if spec.optin:
        return explicit  # opt-in collectors never run under "all"
    return bool("all" in requested or explicit)


def unknown_only_tokens(only: str) -> list[str]:
    """--only tokens that match no collector name, layer, or alias (typos would
    otherwise silently resolve to an empty plan)."""
    known = {"all"}
    for spec in SPECS:
        known |= {spec.name, spec.layer} | set(spec.aliases)
    return [
        token.strip() for token in only.split(",") if token.strip() and token.strip() not in known
    ]


def find_server_pid() -> int | None:
    import psutil

    matches = [
        int(process.info["pid"])
        for process in psutil.process_iter(("pid", "name"))
        if str(process.info.get("name") or "").startswith("7DaysToDieServe")
    ]
    return matches[0] if len(matches) == 1 else None


def tool_version(binary: str) -> str:
    # Same PATH skew as the collector gate: a sudo-only tool (e.g. bpftrace under
    # secure_path) may be absent from the unprivileged PATH, so its recorded
    # version can be empty even though the collector runs. This is metadata only
    # and never gates a capture, so it is left as a best-effort user-PATH lookup.
    path = shutil.which(binary)
    if not path:
        return ""
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, check=False, timeout=5
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0] if lines else ""


def count_samples(artifact: Path) -> int | None:
    if not artifact.is_file() or artifact.suffix == ".data":
        return None
    try:
        with artifact.open("rb") as stream:
            return sum(1 for line in stream if line.strip())
    except OSError:
        return None


def _mono_library(pid: int) -> Path | None:
    maps = Path(f"/proc/{pid}/maps")
    try:
        for line in maps.read_text(errors="replace").splitlines():
            if "libmonobdwgc-2.0.so" in line:
                parts = line.split(None, 5)
                if len(parts) >= 6:
                    return Path(parts[5].strip())
    except OSError:
        pass
    return None


def _bind_mono(session: Path, pid: int, sudo_ok: bool) -> Path | None:
    """Bind-mount the mono .so to a space-free path so uprobes can attach.

    Requires root; mount(2) via sudo is the only option for a non-root APM user.
    """
    source = _mono_library(pid)
    if source is None or not source.is_file() or not sudo_ok:
        return None
    link = session / "runtime/libmonobdwgc-2.0.so"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.touch()
    mounted = subprocess.run(["sudo", "-n", "mount", "--bind", str(source), str(link)], check=False)
    return link if mounted.returncode == 0 else None


def _unmount_mono(link: Path | None) -> None:
    if link is None:
        return
    # This runs in run_capture's finally: it must never raise (a TimeoutExpired
    # or OSError here would abort cleanup AND leak the mount). Warn instead.
    stderr = ""
    try:
        result = subprocess.run(
            ["sudo", "-n", "umount", str(link)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return
        stderr = (result.stderr or "").strip()
        # Busy target (e.g. perf still mapping the .so): a lazy unmount detaches it
        # once the last user exits, so prune's rmtree does not hit EBUSY forever.
        lazy = subprocess.run(
            ["sudo", "-n", "umount", "-l", str(link)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if lazy.returncode == 0:
            return
        stderr = f"{stderr}; lazy: {(lazy.stderr or '').strip()}"
    except (subprocess.TimeoutExpired, OSError) as error:
        stderr = str(error)
    # A left-behind bind mount blocks the next run's mount; make it loud.
    print(
        f"WARNING: failed to unmount mono bind {link}: {stderr} "
        "- unmount manually before the next capture",
        file=sys.stderr,
    )


def telnet_command(host: str, port: int, password: str, command: str) -> bool:
    """Fire one console command over the dedicated telnet interface."""
    import socket
    import time as time_module

    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            sock.settimeout(2)
            with suppress(TimeoutError, OSError):
                sock.recv(4096)
            if password:
                sock.sendall((password + "\n").encode())
                time_module.sleep(0.3)
                with suppress(TimeoutError, OSError):
                    sock.recv(4096)
            sock.sendall((command + "\n").encode())
            time_module.sleep(0.3)
            sock.sendall(b"exit\n")
        return True
    except OSError:
        return False


def telnet_exec(
    host: str, port: int, password: str, command: str, read_seconds: float = 2.0
) -> str:
    """Run one console command and return the accumulated output text."""
    import socket
    import time as time_module

    chunks: list[bytes] = []
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            sock.settimeout(0.5)

            def drain(seconds: float) -> None:
                deadline = time_module.monotonic() + seconds
                while time_module.monotonic() < deadline:
                    with suppress(TimeoutError, OSError):
                        data = sock.recv(8192)
                        if data:
                            chunks.append(data)

            drain(0.8)
            if password:
                sock.sendall((password + "\n").encode())
                drain(0.5)
            sock.sendall((command + "\n").encode())
            drain(read_seconds)
            sock.sendall(b"exit\n")
    except OSError:
        return ""
    return b"".join(chunks).decode("utf-8", "replace")


def rally_players(host: str, port: int, password: str, at: tuple[int, int] | None = None) -> int:
    """Cluster the cohort: teleport everyone to the first player, or to fresh
    coordinates when `at` is given (long campaigns saturate the spawn grid
    around old positions with gore blocks and loot bags).

    Keeps the loaded-chunk union small so AI/combat cost is measurable without
    chunk-streaming noise.
    """
    import re

    listing = telnet_exec(host, port, password, "listplayers")
    players = re.findall(r"\d+\. id=(\d+),.*?pos=\(([-\d.]+), ([-\d.]+), ([-\d.]+)\)", listing)
    if not players:
        return 0
    if at is not None:
        base_x, base_z = float(at[0]), float(at[1])
        base_y = -1.0  # -1: let the server find ground
        anchors = players
    else:
        if len(players) < 2:
            return 0
        _, x, y, z = players[0]
        base_x, base_z = float(x), float(z)
        base_y = float(y)
        anchors = players[1:]
    moved = 0
    for pid, *_ in anchors:
        offset_x = base_x + (moved % 10) * 6 - 15
        offset_z = base_z + (moved // 10) * 6 - 15
        telnet_command(
            host,
            port,
            password,
            f"teleportplayer {pid} {offset_x:.0f} {base_y:.0f} {offset_z:.0f}",
        )
        moved += 1
    return moved


def reset_bridge_stats(host: str, port: int, password: str) -> bool:
    """Reset bridge stats so section totals cover exactly this capture window."""
    return telnet_command(host, port, password, "apm reset")


def _warn(session: Path, message: str) -> None:
    print(f"WARN: {message}", file=sys.stderr)
    with (session / "WARN.txt").open("a") as stream:
        stream.write(message + "\n")


def _result(
    ctx: CaptureContext,
    spec: CollectorSpec,
    status: str,
    *,
    exit_code: int | None = None,
    duration: float = 0.0,
    message: str = "",
) -> CollectorResult:
    artifact = ctx.session / spec.artifact
    produced = artifact.is_file() and artifact.stat().st_size > 0
    result = CollectorResult(
        name=spec.name,
        layer=spec.layer,
        status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        duration_seconds=max(0.0, duration),
        tool=spec.tool,
        tool_version=tool_version(spec.tool),
        sample_count=count_samples(artifact),
        artifacts=[spec.artifact] if produced else [],
        message=message,
    )
    atomic_json(artifact.parent / f"{spec.name}.result.json", schema_dict(result))
    return result


@dataclass
class _Running:
    spec: CollectorSpec
    process: subprocess.Popen[bytes]
    started: float
    streams: list[BinaryIO] = field(default_factory=list)
    finished: float | None = None


@dataclass
class CaptureOutcome:
    session: Path
    results: list[CollectorResult] = field(default_factory=list)
    interrupted: bool = False
    exit_code: int = 0


def _terminate(running: list[_Running]) -> None:
    """Best-effort bounded shutdown.

    Root-owned children (sudo bpftrace) cannot be signaled by a non-root
    parent; they self-terminate via their own `timeout` wrapper, so every wait
    here is bounded to avoid hanging on them.
    """
    for item in running:
        if item.process.poll() is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(item.process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    for item in running:
        if item.process.poll() is None:
            with suppress(subprocess.TimeoutExpired):
                item.process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if item.process.poll() is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(item.process.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                item.process.wait(timeout=5)
        if item.finished is None and item.process.poll() is not None:
            item.finished = time.monotonic()


def _telnet_password_warning(only: str, no_app: bool, telnet_password: str) -> str | None:
    """Fail loudly at start instead of after a full window of failed scrapes:
    7dtd telnet requires the password from SEVENDTD_TELNET_PASSWORD, and
    without it every app-layer record is an auth failure rather than bridge
    telemetry. Host-only layers are unaffected, so this stays a warning."""
    app_spec = next(spec for spec in SPECS if spec.name == "app")
    if not no_app and wanted(app_spec, only) and not telnet_password:
        return (
            "SEVENDTD_TELNET_PASSWORD not set; telnet login will fail "
            "(set it via the environment, never argv)"
        )
    return None


def run_capture(
    *,
    seconds: int,
    pid: int | None,
    only: str,
    no_app: bool,
    telnet_host: str,
    telnet_port: int,
    telnet_password: str,
    finalize: bool = True,
    reset_bridge: bool = False,
    symbolize: bool = True,
) -> CaptureOutcome:
    require_backends()
    if pid is None:
        pid = find_server_pid()
    if pid is None or not Path(f"/proc/{pid}").is_dir():
        raise RuntimeError("no unique running 7DaysToDieServe process; pass --pid")

    root = apm_root()
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    # Exclusive-create claim: an overlapping duplicate capture (retry after a
    # hung first attempt, cron overlap) in the same second must get its own
    # directory instead of interleaving collectors' output into this one.
    session = claim_dir(root / f"session_{stamp}_pid{pid}")
    for sub in ("app", "runtime", "threads", "sync", "scheduler", "cpu", "memory", "io", "bt"):
        (session / sub).mkdir(parents=True, exist_ok=True)
    # Raw sessions capture the server log stream (player names/IPs/SteamIDs via the
    # telnet drain), unlike the scrubbed export bundle - keep them owner-only on
    # shared hosts.
    with suppress(OSError):
        root.chmod(0o700)
        session.chmod(0o700)
    outcome = CaptureOutcome(session=session)

    if message := _telnet_password_warning(only, no_app, telnet_password):
        _warn(session, message)

    sudo_ok = (
        shutil.which("sudo") is not None
        and subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode == 0
    )
    comm = "7DaysToDieServe"
    exe = ""
    cmdline = ""
    thread_count = 0
    try:
        exe = os.path.realpath(f"/proc/{pid}/exe")
        cmdline = (
            Path(f"/proc/{pid}/cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode("utf-8", "replace")
        )
        thread_count = len(list(Path(f"/proc/{pid}/task").iterdir()))
    except OSError:
        pass
    started_at = datetime.now(UTC)
    meta = MetaV2(
        utc=started_at,
        pid=pid,
        comm=comm,
        seconds=seconds,
        only=only,
        no_app=no_app,
        exe=exe,
        cmdline=cmdline,
        threads=thread_count,
        uname=os.uname().release,
        analyzer_version=ANALYZER_VERSION,
        capture_preset=only,
        layers=["app", "runtime", "threads", "sync", "scheduler", "cpu", "memory", "io", "net"],
        tool_versions={name: tool_version(name) for name in ("perf", "bpftrace", "python3")},
    )
    atomic_json(session / "meta.json", schema_dict(meta))

    if symbolize:
        # Force-JIT hot methods and export /tmp/perf-<pid>.map before the
        # window so perf resolves managed frames and the JIT burst stays
        # outside the measurement. This is INDEPENDENT of reset_bridge: without
        # the map, every ustack collector (alloc, futex, sched) resolves managed
        # frames to raw addresses, defeating the tool's core attribution goal.
        # The burst runs pre-window (off the measurement); disable with
        # --no-symbolize only when you deliberately want a faster, unresolved run.
        telnet_command(telnet_host, telnet_port, telnet_password, "apm jitmap full")
        # perf hardcodes /tmp/perf-<pid>.map; the bridge writes the map to its
        # disk-backed telemetry dir, we place only a symlink in tmpfs. The full
        # JIT burst can take tens of seconds; poll until the file stops growing.
        map_source = (
            Path(os.path.realpath(f"/proc/{pid}/exe")).parent
            / f"Mods/7dtd-apm-bridge/telemetry/perf-{pid}.map"
        )
        deadline = time.monotonic() + 90
        last_size = -1
        while time.monotonic() < deadline:
            try:
                size = map_source.stat().st_size
            except OSError:
                size = 0  # not written yet, or removed mid-poll
            if size and size == last_size:
                break
            last_size = size
            time.sleep(2)
        if map_source.is_file() and map_source.stat().st_size:
            map_link = Path(f"/tmp/perf-{pid}.map")
            with suppress(OSError):
                map_link.unlink(missing_ok=True)
                map_link.symlink_to(map_source)
            # Keep a copy in-session so finalize can annotate JIT addresses in
            # any bpftrace output (allocation/stall sites), not just perf flames.
            with suppress(OSError):
                shutil.copy2(map_source, session / "runtime" / f"perf-{pid}.map")
            with map_source.open(errors="replace") as handle:
                symbols = sum(1 for _ in handle)
            print(f">> jitmap: {symbols} managed symbols mapped")
        else:
            _warn(session, "bridge jitmap export failed; managed perf frames stay [jit]")

    if reset_bridge:
        # Reset section/GC totals so they cover only this window (vs server uptime).
        if reset_bridge_stats(telnet_host, telnet_port, telnet_password):
            print(">> bridge stats reset; section totals cover this capture window")
        else:
            _warn(session, "bridge stat reset failed; section totals span server uptime")

    mono_link = _bind_mono(session, pid, sudo_ok)
    if mono_link is None:
        _warn(session, "mono .so not bound; GC uprobes disabled")
    ctx = CaptureContext(
        session=session,
        pid=pid,
        comm=comm,
        seconds=seconds,
        telnet_host=telnet_host,
        telnet_port=telnet_port,
        telnet_password=telnet_password,
        mono_so=mono_link,
        sudo_ok=sudo_ok,
    )

    def _sigterm(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    running: list[_Running] = []
    previous_sigterm = signal.signal(signal.SIGTERM, _sigterm)
    try:
        try:
            _launch_collectors(ctx, only, no_app, running)
            deadline = time.monotonic() + seconds + max(GRACE_SECONDS, seconds)
            while any(item.finished is None for item in running):
                for item in running:
                    if item.finished is None and item.process.poll() is not None:
                        item.finished = time.monotonic()
                if time.monotonic() >= deadline:
                    _terminate(running)
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            outcome.interrupted = True
            _terminate(running)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        for item in running:
            for stream in item.streams:
                # A flush failure here (e.g. disk full) must not skip the unmount
                # below and leak the bind mount into the next capture.
                with suppress(OSError):
                    stream.close()
        _unmount_mono(mono_link)
    if any(item.process.poll() is None for item in running):
        _warn(
            session,
            "some root-owned collectors are still draining until their own timeout",
        )

    for item in running:
        rc = item.process.poll()
        duration = (item.finished or time.monotonic()) - item.started
        if rc is None:
            status, message = (
                ("interrupted", "capture interrupted; collector still draining")
                if outcome.interrupted
                else ("failed", "collector did not exit within the grace window")
            )
        elif outcome.interrupted and rc not in (0, 124, 130):
            status, message = "interrupted", "capture interrupted before completion"
        elif rc in (126, 127):
            status, message = "unavailable", "collector tool missing or not permitted"
        elif (
            (session / item.spec.artifact).is_file()
            and (session / item.spec.artifact).stat().st_size
            and rc in (0, 124, 130)
        ):
            status, message = "ok", "expected timeout" if rc else ""
        elif rc in (0, 124, 130):
            status, message = "failed", "collector exited cleanly but produced no output"
        else:
            status, message = "failed", f"collector exited unexpectedly (rc={rc})"
        result = _result(ctx, item.spec, status, exit_code=rc, duration=duration, message=message)
        outcome.results.append(result)
        print(f"   {item.spec.name}: {status} exit={rc} samples={result.sample_count}")

    _ingest_bridge_snapshot(session, pid, no_app, started_at, seconds)

    folded = session / "cpu/perf/stacks.folded"
    if folded.is_file() and folded.stat().st_size:
        # A hung flame build must not block finalize.
        with suppress(subprocess.TimeoutExpired, OSError):
            subprocess.run(
                [
                    "bash",
                    str(EBPF / "make_flames.sh"),
                    str(session / "cpu/perf"),
                    f"7DTD APM pid={pid}",
                ],
                check=False,
                timeout=180,
            )

    if finalize:
        from .finalize import finalize as finalize_session
        from .session import audit_session

        finalize_result = finalize_session(session)
        _, valid = audit_session(session)
        outcome.exit_code = finalize_result.exit_code or (0 if valid else 1)
    if outcome.interrupted:
        outcome.exit_code = 130
    _auto_prune_sessions()
    return outcome


def _auto_prune_sessions() -> None:
    """Sessions accumulate tens of MB each and nothing pruned them automatically -
    a 24/7 host with periodic captures grows without bound. Keep the newest
    APM_KEEP_SESSIONS (default 40; <= 0 disables) via the shared retention policy.
    Never prunes the just-created session (it is the newest)."""
    keep = keep_sessions_budget()
    if keep <= 0:
        return
    grace = prune_grace_hours()
    for old, error in remove_sessions(
        sessions_beyond_budget(list_sessions(apm_root()), keep), grace
    ):
        if error is None:
            print(f"pruned old session {old.name} (APM_KEEP_SESSIONS={keep})", file=sys.stderr)
        else:
            print(f"WARNING: prune failed for {old.name}: {error}", file=sys.stderr)
    for entry, error in purge_expired_trash(apm_root(), grace):
        if error is not None:
            print(f"WARNING: trash purge failed for {entry.name}: {error}", file=sys.stderr)


def _launch_collectors(
    ctx: CaptureContext, only: str, no_app: bool, running: list[_Running]
) -> None:
    session, seconds = ctx.session, ctx.seconds
    sudo_ok = ctx.sudo_ok
    for spec in SPECS:
        if not wanted(spec, only) or (spec.name == "app" and no_app):
            _result(ctx, spec, "skipped", message="collector not requested")
            continue
        if spec.needs_sudo and not sudo_ok:
            _result(ctx, spec, "unavailable", message="sudo -n unavailable for eBPF")
            _warn(session, f"{spec.name}: sudo -n failed; eBPF collector skipped")
            continue
        # A sudo-run collector resolves its tool via sudoers secure_path, which
        # often differs from the unprivileged PATH (bpftrace is commonly
        # /usr/sbin/bpftrace). Do not hard-skip such a collector on a failed
        # user-PATH which(); the 126/127 return-code check classifies a tool that
        # is genuinely missing or not permitted once the sudo command runs.
        if not spec.needs_sudo and not shutil.which(spec.tool):
            _result(ctx, spec, "unavailable", message=f"{spec.tool} not installed")
            continue
        command = spec.build(ctx)
        if command is None:
            _result(ctx, spec, "unavailable", message="collector prerequisites missing")
            continue
        env = os.environ.copy()
        if spec.env:
            env.update(spec.env(ctx))
        artifact = session / spec.artifact
        streams: list[BinaryIO] = []
        try:
            artifact.parent.mkdir(parents=True, exist_ok=True)
            stdout: BinaryIO | int
            if spec.tool == "bpftrace":
                stdout = artifact.open("wb")
            elif spec.stdout_to:
                target = session / spec.stdout_to
                target.parent.mkdir(parents=True, exist_ok=True)
                stdout = target.open("wb")
            else:
                stdout = subprocess.DEVNULL
            if not isinstance(stdout, int):
                streams.append(stdout)
            stderr = (artifact.parent / f"{spec.name}.err").open("wb")
            streams.append(stderr)
            print(f">> [{spec.layer}] {spec.name} ({seconds}s)")
            process = subprocess.Popen(
                command, stdout=stdout, stderr=stderr, env=env, start_new_session=True
            )
        except OSError as error:
            # An open/Popen failure here (disk full, EMFILE, bad path) must not
            # leak the already-opened descriptors nor abort the remaining
            # pipeline: close what was opened, record, keep launching.
            for stream in streams:
                with suppress(OSError):
                    stream.close()
            _result(ctx, spec, "failed", message=f"collector failed to start: {error}")
            _warn(session, f"{spec.name}: failed to start: {error}")
            continue
        running.append(
            _Running(spec=spec, process=process, started=time.monotonic(), streams=streams)
        )


def _ingest_bridge_snapshot(
    session: Path, pid: int, no_app: bool, started_at: datetime, seconds: int
) -> None:
    """Copy the in-game bridge snapshot only when its content overlaps the window.

    Validation is content-based (schema v3 + embedded utc), never file mtime.
    """
    if no_app:
        return
    try:
        exe = Path(os.path.realpath(f"/proc/{pid}/exe"))
    except OSError:
        return
    latest = exe.parent / "Mods/7dtd-apm-bridge/telemetry/apm_app_latest.json"
    if not latest.is_file():
        return
    try:
        snapshot = BridgeSnapshotV3.model_validate(json.loads(latest.read_text()))
    except (json.JSONDecodeError, OSError, ValidationError) as error:
        _warn(session, f"bridge snapshot rejected by schema validation: {error}")
        return
    stamp = snapshot.utc if snapshot.utc.tzinfo else snapshot.utc.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - stamp).total_seconds()
    if stamp < started_at or age > seconds + 15:
        _warn(session, "bridge snapshot does not overlap the capture window")
        return
    if not snapshot.sections:
        _warn(session, "bridge snapshot has no managed sections; not ingested")
        return
    shutil.copy2(latest, session / "app/apm_app.json")
    print(">> [app] ingested schema-validated bridge snapshot")


def write_plan_text(ctx_args: dict[str, object], only: str) -> str:
    """Dry-run helper: resolved collector plan without side effects."""
    lines = [f"capture plan (only={only}):"]
    for spec in SPECS:
        state = "run" if wanted(spec, only) else "skip"
        lines.append(f"  {state:4s} {spec.name:8s} layer={spec.layer} -> {spec.artifact}")
    lines.append(f"args: {json.dumps(ctx_args, default=str)}")
    return "\n".join(lines)


__all__ = [
    "SPECS",
    "CaptureContext",
    "CaptureOutcome",
    "CollectorSpec",
    "find_server_pid",
    "run_capture",
    "unknown_only_tokens",
    "wanted",
    "write_plan_text",
]
