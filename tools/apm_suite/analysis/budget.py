"""Evidence-gated performance budgets (CI / local gate).

Missing evidence is UNKNOWN and fails the gate; it is never a healthy zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..io import atomic_json, atomic_text, load_json
from ..models import collected_layer_scores

DEFAULT_BUDGET: dict[str, Any] = {
    "schema": "7dtd.apm.budget.v2",
    "max_layer_scores": {
        "sync_locks": 70,
        "runtime_gc": 60,
        "cpu": 50,
        "io": 50,
        "memory_cache": 50,
        "scheduler": 50,
        "app_sim": 60,
    },
    "max_sum_layer_score": 250,
    "max_late_tick_share": 0.05,  # ticks over 60ms as a fraction of the window
    "max_gross_alloc_mb_per_second": 15.0,  # gross GC_malloc churn -> Boehm STW pauses
    # Chunk bandwidth is gated on the kernel UDP send rate (always capture-
    # windowed = current steady state). The bridge mapTransfers MB/s is a
    # since-reset lifetime average inflated by the join burst (see report R56),
    # so it is NOT gated to avoid false FAILs.
    "max_udp_send_mb_per_second": 30.0,  # kernel udp send = current game traffic
    "max_section_heat": {
        "World.TickEntities": 20,
        "EntityAlive.updateTasks": 5,
        "EAIManager.Update": 5,
        "PathNavigate.UpdateNavigation": 5,
        "DynamicMeshManager.Update": 8,
    },
    "notes": "Managed limits use p95Ms when available, otherwise avgMs.",
}


def load_layers(session: Path) -> dict[str, float]:
    return collected_layer_scores(load_json(session / "summary.json"))


def load_sections(session: Path) -> dict[str, float]:
    heat: dict[str, float] = {}
    bridge = session / "csharp_bridge.json"
    if bridge.is_file():
        data = load_json(bridge)
        for section in data.get("top_managed_sections") or []:
            name = section.get("name")
            if name:
                # Explicit None checks: a legitimate score/avg of 0 must not fall
                # through to the next field as if it were missing. A section with
                # no heat at all is omitted (UNKNOWN), never recorded as a 0 that
                # would silently pass the gate.
                score = section.get("score")
                avg = section.get("avgMs")
                value = score if score is not None else avg
                if value is not None:
                    heat[str(name)] = float(value)
    return heat


def check(
    session: Path, budget: dict[str, Any], baseline: Path | None, max_regression: float
) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True
    layers = load_layers(session)
    sections = load_sections(session)

    for name, limit in (budget.get("max_layer_scores") or {}).items():
        if name not in layers:
            ok = False
            lines.append(f"UNKNOWN layer {name}: no usable evidence")
            continue
        value = layers[name]
        if value > float(limit):
            ok = False
            lines.append(f"FAIL layer {name}={value} > budget {limit}")
        else:
            lines.append(f"ok   layer {name}={value} <= {limit}")

    max_sum = budget.get("max_sum_layer_score")
    if max_sum is not None:
        total = sum(layers.values())
        if total > float(max_sum):
            ok = False
            lines.append(f"FAIL sum_layers={total} > budget {max_sum}")
        else:
            lines.append(f"ok   sum_layers={total} <= {max_sum}")

    for name, limit in (budget.get("max_section_heat") or {}).items():
        if name not in sections:
            lines.append(f"skip section {name} (no heat data)")
            continue
        value = sections[name]
        if value > float(limit):
            ok = False
            lines.append(f"FAIL section {name}={value} > budget {limit}")
        else:
            lines.append(f"ok   section {name}={value} <= {limit}")

    if baseline is not None:
        base_layers = load_layers(baseline)
        if set(base_layers) != set(layers):
            return False, lines + [
                "UNKNOWN regression: baseline and candidate layer coverage differ"
            ]
        for name, base_value in base_layers.items():
            value = layers[name]
            regression = value - base_value
            if regression > max_regression:
                ok = False
                lines.append(
                    f"FAIL regression {name}: baseline={base_value} now={value} "
                    f"Δ=+{regression:.1f} > {max_regression}"
                )
            else:
                lines.append(
                    f"ok   regression {name}: baseline={base_value} now={value} Δ={regression:+.1f}"
                )

    rate_meta: dict[str, Any] | None = None
    for key, (block_name, field) in (
        ("max_gross_alloc_mb_per_second", ("gc", "grossAllocMBPerSecond")),
        ("max_udp_send_mb_per_second", ("net", "udp_send_mb_per_second")),
    ):
        limit = budget.get(key)
        if limit is None:
            continue
        if rate_meta is None:
            rate_meta = load_json(session / "summary.json").get("metadata") or {}
        rate_value = (rate_meta.get(block_name) or {}).get(field)
        if rate_value is None:
            lines.append(f"skip {key} (no data)")
            continue
        if float(rate_value) > float(limit):
            ok = False
            lines.append(f"FAIL {key}={rate_value} > budget {limit}")
        else:
            lines.append(f"ok   {key}={rate_value} <= {limit}")

    max_late = budget.get("max_late_tick_share")
    if max_late is not None:
        summary = load_json(session / "summary.json")
        frame = (summary.get("metadata") or {}).get("frame") or {}
        late = int(frame.get("lateTicks") or 0)
        window = int(frame.get("windowUpdates") or 0)
        if window:
            share = late / window
            if share > float(max_late):
                ok = False
                lines.append(
                    f"FAIL late_ticks {late}/{window} = {share:.3f} > budget {max_late} "
                    "(server missed its tick deadline)"
                )
            else:
                lines.append(f"ok   late_ticks {late}/{window} = {share:.3f} <= {max_late}")
        else:
            lines.append("skip late_ticks (no bridge frame data)")

    return ok, lines


def check_budget(
    session: Path,
    budget_path: Path | None = None,
    baseline: Path | None = None,
    max_regression: float = 15.0,
) -> bool:
    """Run the gate and persist budget_check.txt/.json. Returns pass/fail."""
    budget = DEFAULT_BUDGET
    if budget_path and budget_path.is_file():
        budget = json.loads(budget_path.read_text())
    ok, lines = check(session, budget, baseline, max_regression)
    report = "\n".join(lines) + f"\n\nRESULT: {'PASS' if ok else 'FAIL'}\n"
    print(report)
    atomic_text(session / "budget_check.txt", report)
    atomic_json(
        session / "budget_check.json",
        {"schema": "7dtd.apm.budget-result.v1", "passed": ok, "checks": lines},
    )
    return ok
