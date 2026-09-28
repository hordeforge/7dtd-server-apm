"""Composite health score (0-100, higher = healthier) from summary layer scores.

health.json is the single home of health; summary.json is never patched.
Grades are withheld below 80% weighted coverage.
"""

from __future__ import annotations

from pathlib import Path

from ..io import atomic_json, load_json
from ..models import HealthGrade, HealthV2, collected_layer_scores, schema_dict

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
# Lower bound of each grade band, best first; anything below the last is F.
GRADE_BANDS: tuple[tuple[float, HealthGrade], ...] = (
    (85.0, "A"),
    (70.0, "B"),
    (55.0, "C"),
    (40.0, "D"),
)


def grade_for(health: float) -> HealthGrade:
    for minimum, grade in GRADE_BANDS:
        if health >= minimum:
            return grade
    return "F"


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
    rounded = round(health, 2)
    return HealthV2(
        health=rounded,
        pressure=round(pressure, 2),
        # Graded on the value that is stored, not the unrounded float: the band
        # edges are inclusive and declared to 2 decimals, so grading the raw
        # number would write health=85.0 next to grade="B".
        grade=grade_for(rounded),
        coverage=min(coverage, 1.0),
        confidence="medium",
        detail=detail,
    )


def build_health(session: Path) -> HealthV2:
    summary = load_json(session / "summary.json")
    result = compute_health(collected_layer_scores(summary))
    result.session = session.name
    atomic_json(session / "health.json", schema_dict(result))
    return result
