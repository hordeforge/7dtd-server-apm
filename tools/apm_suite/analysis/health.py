"""Composite health score (0-100, higher = healthier) from summary layer scores.

health.json is the single home of health; summary.json is never patched.
Grades are withheld below 80% weighted coverage.
"""

from __future__ import annotations

from pathlib import Path

from ..io import atomic_json, load_json
from ..models import HealthV2, schema_dict

# Weights sum ~1.0 for known layers
WEIGHTS = {
    "sync_locks": 0.18,
    "runtime_gc": 0.15,
    "cpu": 0.15,
    "app_sim": 0.15,
    "io": 0.12,
    "memory_cache": 0.12,
    "scheduler": 0.13,
}
DEFAULT_WEIGHT = 0.08
COVERAGE_MIN = 0.8


def layers_from_summary(summary: dict[str, object]) -> dict[str, float]:
    layers = summary.get("layers")
    out: dict[str, float] = {}
    for layer in layers if isinstance(layers, list) else []:
        if (
            layer.get("layer")
            and layer.get("state", "collected") == "collected"
            and layer.get("score") is not None
        ):
            out[str(layer["layer"])] = float(layer["score"])
    return out


def compute_health(layers: dict[str, float]) -> HealthV2:
    if not layers:
        return HealthV2(reason="no collected layers with usable evidence")
    weighted = 0.0
    weight_sum = 0.0
    detail: dict[str, dict[str, float]] = {}
    for name, score in layers.items():
        weight = WEIGHTS.get(name, DEFAULT_WEIGHT)
        pressure = max(0.0, min(100.0, score))
        weighted += weight * pressure
        weight_sum += weight
        detail[name] = {"pressure": pressure, "weight": weight}
    coverage = round(weight_sum / sum(WEIGHTS.values()), 3)
    if coverage < COVERAGE_MIN:
        return HealthV2(
            coverage=min(coverage, 1.0),
            reason="less than 80% of weighted layers contain usable evidence",
            detail=detail,
        )
    pressure = weighted / weight_sum if weight_sum else 0.0
    health = max(0.0, min(100.0, 100.0 - pressure))
    grade = (
        "A"
        if health >= 85
        else "B"
        if health >= 70
        else "C"
        if health >= 55
        else "D"
        if health >= 40
        else "F"
    )
    return HealthV2(
        health=round(health, 2),
        pressure=round(pressure, 2),
        grade=grade,  # type: ignore[arg-type]
        coverage=min(coverage, 1.0),
        confidence="medium",
        detail=detail,
    )


def build_health(session: Path) -> HealthV2:
    summary = load_json(session / "summary.json")
    result = compute_health(layers_from_summary(summary))
    result.session = session.name
    atomic_json(session / "health.json", schema_dict(result))
    return result
