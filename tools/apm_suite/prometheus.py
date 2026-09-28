"""Prometheus text exposition of one finalized session.

Reads the session artifacts the way every other reader does (summary, health,
bridge attribution) and emits the gauge set a Prometheus scrape consumes. Kept
out of cli.py so the metric contract has one owner; the command owns only the
argument parsing and the message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .io import atomic_text, load_json
from .models import as_number, layer_signals


class MetricError(Exception):
    """The session is not exportable: a required artifact is missing or unreadable."""


def _prom_label(value: object) -> str:
    """Escape a Prometheus label value per spec (\\ then " then newline). Current
    label sources are fixed internal names, but a metrics exporter must never
    emit a value that could break the line format."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def export_metrics(session: Path, output: Path) -> None:
    """Write the Prometheus text exposition of a finalized session to `output`.

    Metric names are a deployed contract: scrape configs and dashboards refer
    to them across upgrades, so the name set has one home.
    """
    summary_path = session / "summary.json"
    if not summary_path.is_file():
        raise MetricError("session has no summary.json")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        # OSError: the summary vanished or became unreadable after the
        # is_file() gate; name it like every other unreadable-input error here.
        raise MetricError(f"unreadable {summary_path}: {error}") from None
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
    health: dict[str, object] = {}
    if health_path.is_file():
        try:
            health = load_json(health_path)
        except ValueError as error:
            raise MetricError(f"unreadable {health_path}: {error}") from None
    if not health:
        health = summary.get("health") or {}
    coverage = as_number(health.get("coverage"))
    if coverage is not None:
        lines += [
            "# TYPE sevendtd_apm_coverage gauge",
            f"sevendtd_apm_coverage {coverage:.6f}",
        ]
    bridge_path = session / "csharp_bridge.json"
    attribution: dict[str, Any] = {}
    if bridge_path.is_file():
        try:
            attribution = load_json(bridge_path).get("attribution") or {}
        except ValueError as error:
            raise MetricError(f"unreadable {bridge_path}: {error}") from None
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
    gc_layer = layer_signals(summary, "runtime_gc")
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
