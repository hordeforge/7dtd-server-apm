"""Detect super-linear code by fitting cost vs load across a ladder of captures.

A section whose PER-CALL cost grows faster than the load (exponent > ~1.3) is
algorithmically super-linear (e.g. an O(players^2) per-tick update). A section
whose TOTAL cost grows super-linearly is a scaling wall even if each call is
cheap. Both are ranked so the worst-scaling code surfaces first.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..io import load_json
from ..models import as_number

# exponent (slope of log(cost) vs log(load)) thresholds
LINEAR_LOW, LINEAR_HIGH = 0.8, 1.2
SUPERLINEAR = 1.3
QUADRATIC = 1.7


def _loglog_slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of log(y) vs log(x); the scaling exponent. None if
    fewer than 3 usable (x>0, y>0) points or x has no spread."""
    pts = [(math.log(x), math.log(y)) for x, y in points if x > 0 and y > 0]
    if len(pts) < 3:
        return None
    n = len(pts)
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    return (n * sxy - sx * sy) / denom


def classify(exponent: float) -> str:
    if exponent >= QUADRATIC:
        return "quadratic+"
    if exponent >= SUPERLINEAR:
        return "super-linear"
    if exponent >= LINEAR_LOW:
        return "linear"
    return "sub-linear"


def _scale_of(summary: dict[str, Any], key: str) -> float:
    world = (summary.get("metadata") or {}).get("world") or {}
    if key == "entities":
        return as_number(world.get("entities")) or 0.0
    return as_number(world.get("clients")) or as_number(world.get("players")) or 0.0


def _sections(session: Path) -> dict[str, dict[str, float]]:
    bridge = session / "csharp_bridge.json"
    if not bridge.is_file():
        return {}
    out: dict[str, dict[str, float]] = {}
    for s in load_json(bridge).get("top_managed_sections") or []:
        name = s.get("name")
        avg = as_number(s.get("avgMs"))
        total = as_number(s.get("totalMs"))
        if name and avg is not None and total is not None:
            out[str(name)] = {"avgMs": avg, "totalMs": total}
    return out


def analyze_scaling(sessions: list[Path], scale_key: str = "players") -> dict[str, Any]:
    """Fit each section's per-call and total cost against the load across
    sessions and rank by scaling exponent (worst first)."""
    points: list[tuple[float, dict[str, dict[str, float]]]] = []
    for session in sessions:
        summary_path = session / "summary.json"
        if not summary_path.is_file():
            continue
        scale = _scale_of(load_json(summary_path), scale_key)
        if scale > 0:
            points.append((scale, _sections(session)))
    points.sort(key=lambda p: p[0])
    scales = [p[0] for p in points]

    names = {name for _, secs in points for name in secs}
    findings: list[dict[str, Any]] = []
    for name in names:
        per_call = [(scale, secs[name]["avgMs"]) for scale, secs in points if name in secs]
        total = [(scale, secs[name]["totalMs"]) for scale, secs in points if name in secs]
        call_exp = _loglog_slope(per_call)
        total_exp = _loglog_slope(total)
        if call_exp is None and total_exp is None:
            continue
        findings.append(
            {
                "section": name,
                "per_call_exponent": round(call_exp, 2) if call_exp is not None else None,
                "per_call_class": classify(call_exp) if call_exp is not None else "n/a",
                "total_exponent": round(total_exp, 2) if total_exp is not None else None,
                "total_class": classify(total_exp) if total_exp is not None else "n/a",
                "points": len(per_call),
            }
        )
    # Rank by the worse of the two exponents (algorithmic super-linearity first).
    findings.sort(key=lambda f: -max(f["per_call_exponent"] or 0, f["total_exponent"] or 0))
    super_linear = [
        f
        for f in findings
        if (f["per_call_exponent"] or 0) >= SUPERLINEAR or (f["total_exponent"] or 0) >= SUPERLINEAR
    ]
    return {
        "schema": "7dtd.apm.scaling.v1",
        "scale_key": scale_key,
        "scales": scales,
        "sections": findings,
        "super_linear": super_linear,
    }
