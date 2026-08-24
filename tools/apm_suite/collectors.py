"""Collector catalog: the single source of truth for what a capture can run.

Every collector is one CollectorSpec. Token resolution (--only) goes through
models.collector_requested so capture planning, the session audit, and summary
scoring cannot disagree about what a token means (see models.LAYER_ALIASES).
This module is a leaf: it imports only paths/models, never session/capture,
so both orchestration and the audit store can consume the same catalog.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import LAYER_ALIASES, collector_requested
from .paths import APM_BACKENDS, TOOLS

HOST_PROFILER = TOOLS / "host_profiler"


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
    build: Callable[[CaptureContext], list[str] | None]  # None => unavailable
    # Tokens beyond the collector name and its layer's aliases that request
    # this collector specifically: cross-layer ride-alongs ("threads" also
    # pulls proc sampling) or forensic opt-in tokens for mono_alloc.
    extra_aliases: frozenset[str] = frozenset()
    needs_sudo: bool = False
    env: Callable[[CaptureContext], dict[str, str]] | None = None
    stdout_to: str | None = None  # session-relative stdout tee target
    optin: bool = False  # never run under "all" or a layer token; explicit name/alias only

    def requested(self, tokens: set[str]) -> bool:
        return collector_requested(
            self.name, self.layer, tokens, extra_aliases=self.extra_aliases, optin=self.optin
        )


def _bt(spec_name: str, script: Path) -> Callable[[CaptureContext], list[str] | None]:
    def build(ctx: CaptureContext) -> list[str] | None:
        prepared = ctx.session / "bt" / f"{spec_name}.bt"
        cmd = [
            sys.executable,
            str(HOST_PROFILER / "preprocess_bt.py"),
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


def _mono_probe(name: str) -> Callable[[CaptureContext], list[str] | None]:
    """Mono uprobe probes additionally need the bound libmonobdwgc path."""

    def build(ctx: CaptureContext) -> list[str] | None:
        if ctx.mono_so is None:
            return None
        return _bt(name, APM_BACKENDS / f"collectors/{name}.bt")(ctx)

    return build


_mono_gc = _mono_probe("mono_gc")
_mono_alloc = _mono_probe("mono_alloc")


SPECS: tuple[CollectorSpec, ...] = (
    CollectorSpec(
        name="app",
        layer="app_sim",
        artifact="app/bridge.jsonl",
        tool="python3",
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
        extra_aliases=frozenset({"threads"}),  # thread counts ride along with a threads request
        build=lambda ctx: [
            sys.executable,
            str(HOST_PROFILER / "proc_sample.py"),
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
        build=lambda ctx: [
            "bash",
            str(HOST_PROFILER / "perf_record.sh"),
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
        build=_bt("oncpu", APM_BACKENDS / "collectors/cpu_profile.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="runqlat",
        layer="scheduler",
        artifact="scheduler/runqlat.bt.out",
        tool="bpftrace",
        build=_bt("runqlat", APM_BACKENDS / "collectors/runqlat.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="offcpu",
        layer="scheduler",
        artifact="scheduler/offcpu.bt.out",
        tool="bpftrace",
        build=_bt("offcpu", APM_BACKENDS / "collectors/offcpu.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="states",
        layer="scheduler",
        artifact="scheduler/states.bt.out",
        tool="bpftrace",
        build=_bt("states", APM_BACKENDS / "collectors/sched_states.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="futex",
        layer="sync_locks",
        artifact="sync/futex.bt.out",
        tool="bpftrace",
        build=_bt("futex", APM_BACKENDS / "collectors/futex.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="vfs",
        layer="io",
        artifact="io/vfs.bt.out",
        tool="bpftrace",
        build=_bt("vfs", APM_BACKENDS / "collectors/vfs_lat.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="block",
        layer="io",
        artifact="io/block.bt.out",
        tool="bpftrace",
        build=_bt("block", APM_BACKENDS / "collectors/block_lat.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="io_net",
        layer="io",
        artifact="io/io_net.bt.out",
        tool="bpftrace",
        build=_bt("io_net", APM_BACKENDS / "collectors/io_net.bt"),
        needs_sudo=True,
    ),
    CollectorSpec(
        name="mono_gc",
        layer="runtime_gc",
        artifact="runtime/mono_gc.bt.out",
        tool="bpftrace",
        build=_mono_gc,
        needs_sudo=True,
    ),
    CollectorSpec(
        name="mono_alloc",
        layer="runtime_gc",
        artifact="runtime/mono_alloc.bt.out",
        tool="bpftrace",
        extra_aliases=frozenset({"alloc", "allocsites"}),
        build=_mono_alloc,
        needs_sudo=True,
        optin=True,  # high overhead: opt in with --only alloc (forensic)
    ),
)

SPEC_BY_NAME: dict[str, CollectorSpec] = {spec.name: spec for spec in SPECS}

# Every token a user may pass to --only: "all", collector names, layer names,
# layer aliases, and per-collector extra aliases.
KNOWN_ONLY_TOKENS: frozenset[str] = frozenset(
    {"all"}
    | {spec.name for spec in SPECS}
    | {spec.layer for spec in SPECS}
    | set().union(*LAYER_ALIASES.values())
    | set().union(*(spec.extra_aliases for spec in SPECS))
)


def wanted(spec: CollectorSpec, only: str) -> bool:
    """True when this collector runs under the given --only string."""
    return spec.requested({token.strip() for token in only.split(",") if token.strip()})


def unknown_only_tokens(only: str) -> list[str]:
    """--only tokens that match no collector name, layer, or alias (typos would
    otherwise silently resolve to an empty plan). Empty tokens ("a,,b") and
    an all-whitespace string resolve to no plan rather than a typo error."""
    return [
        token
        for token in (t.strip() for t in only.split(","))
        if token and token not in KNOWN_ONLY_TOKENS
    ]


__all__ = [
    "CaptureContext",
    "CollectorSpec",
    "KNOWN_ONLY_TOKENS",
    "SPEC_BY_NAME",
    "SPECS",
    "unknown_only_tokens",
    "wanted",
]
