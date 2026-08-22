"""Compare two APM sessions (before/after a performance change).

Rejects incompatible sessions (layer coverage, collector selection, duration,
analyzer version, workload manifest) rather than producing misleading deltas.
Lower layer score and lower per-call section heat are better (less pressure).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..io import atomic_json, atomic_text, load_json
from ..models import collected_layer_scores
from .flame_delta import delta, load_weights


def _first_present(*values: Any) -> float:
    """First non-None value as float, else 0.0. Unlike `a or b`, a legitimate 0
    is kept rather than falling through to the next field."""
    for value in values:
        if value is not None:
            return float(value)
    return 0.0


def load_sections(session: Path) -> dict[str, float]:
    """name -> per-call heat (p95/avg ms). Never cumulative totals: bridge
    snapshots accumulate since server start, so totalMs is not comparable
    between sessions."""
    heat: dict[str, float] = {}
    bridge = session / "csharp_bridge.json"
    if bridge.is_file():
        data = load_json(bridge)
        for section in data.get("top_managed_sections") or []:
            name = section.get("name")
            if not name:
                continue
            heat[str(name)] = _first_present(
                section.get("score"), section.get("p95"), section.get("avgMs")
            )
    app = session / "app"
    if app.is_dir():
        for path in list(app.glob("apm_app*.json")) + list(app.glob("snapshot_*.json")):
            try:
                obj = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            for section in obj.get("sections") or []:
                name = section.get("name")
                if not name:
                    continue
                per_call = _first_present(section.get("p95Ms"), section.get("avgMs"))
                if per_call > heat.get(str(name), 0):
                    heat[str(name)] = per_call
    return heat


def layer_map(summary: dict[str, Any]) -> dict[str, float]:
    return collected_layer_scores(summary)


def _flame_path(session: Path) -> Path | None:
    for rel in ("cpu/perf/stacks.annotated.folded", "cpu/perf/stacks.folded", "stacks.folded"):
        path = session / rel
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def load_flame_deltas(a: Path, b: Path, top: int = 20) -> list[dict[str, Any]]:
    """Top frame weight deltas between two sessions' folded stacks."""
    folded_a, folded_b = _flame_path(a), _flame_path(b)
    if not folded_a or not folded_b:
        return []
    return delta(load_weights(folded_a), load_weights(folded_b), top=top)


def _attribution_totals(session: Path) -> dict[str, float]:
    """Window-scoped subsystem totals; only comparable when both sessions
    reset bridge stats at capture start (scenario default)."""
    bridge = session / "csharp_bridge.json"
    if not bridge.is_file():
        return {}
    attribution = load_json(bridge).get("attribution") or {}
    return {
        str(s["subsystem"]): float(s["scaled_total_ms"])
        for s in attribution.get("subsystems") or []
    }


def _winner(delta_value: float) -> str:
    if delta_value < -0.01:
        return "B"
    if delta_value > 0.01:
        return "A"
    return "tie"


def compare_sessions(a: Path, b: Path) -> dict[str, Any]:
    summary_a, summary_b = load_json(a / "summary.json"), load_json(b / "summary.json")
    layers_a, layers_b = layer_map(summary_a), layer_map(summary_b)
    if set(layers_a) != set(layers_b):
        raise ValueError(f"incompatible layer coverage: A={sorted(layers_a)} B={sorted(layers_b)}")
    meta_a, meta_b = summary_a.get("meta") or {}, summary_b.get("meta") or {}
    if meta_a.get("analyzer_version") != meta_b.get("analyzer_version"):
        raise ValueError("analyzer versions differ; re-finalize both sessions with one version")
    if str(meta_a.get("only") or "all") != str(meta_b.get("only") or "all"):
        raise ValueError("incompatible collector selection")
    duration_a = float(meta_a.get("seconds") or 0)
    duration_b = float(meta_b.get("seconds") or 0)
    # A zero/near-zero window is a failed capture; comparing it to a real one
    # yields meaningless deltas. Reject before the 10% check (which is skipped
    # when a duration is falsy).
    if duration_a < 5 or duration_b < 5:
        raise ValueError(
            f"capture too short to compare (durations {duration_a}s vs {duration_b}s); "
            "re-capture both for at least 5s"
        )
    if abs(duration_a - duration_b) / max(duration_a, duration_b) > 0.1:
        raise ValueError(
            f"capture durations differ by more than 10%: {duration_a}s vs {duration_b}s"
        )
    workload_a, workload_b = a / "workload.json", b / "workload.json"
    if workload_a.is_file() != workload_b.is_file():
        raise ValueError("only one session has a workload manifest")
    if workload_a.is_file():
        doc_a, doc_b = json.loads(workload_a.read_text()), json.loads(workload_b.read_text())
        keys = ("mode", "target", "workload")
        if {k: doc_a.get(k) for k in keys} != {k: doc_b.get(k) for k in keys}:
            raise ValueError("workload manifests are not equivalent")

    layer_deltas: list[dict[str, Any]] = []
    better_a = better_b = 0
    for name in sorted(set(layers_a) | set(layers_b)):
        value_a, value_b = layers_a[name], layers_b[name]
        difference = value_b - value_a  # negative => B improved (lower pressure)
        winner = _winner(difference)
        better_a += winner == "A"
        better_b += winner == "B"
        layer_deltas.append(
            {
                "layer": name,
                "a_score": value_a,
                "b_score": value_b,
                "delta_b_minus_a": round(difference, 3),
                "better": winner,
            }
        )
    layer_deltas.sort(key=lambda d: abs(float(d["delta_b_minus_a"])), reverse=True)

    sections_a, sections_b = load_sections(a), load_sections(b)
    section_deltas: list[dict[str, Any]] = []
    for name in sorted(set(sections_a) | set(sections_b)):
        in_a, in_b = name in sections_a, name in sections_b
        value_a, value_b = sections_a.get(name, 0.0), sections_b.get(name, 0.0)
        difference = value_b - value_a
        # A section measured in only one session is NOT an improvement/regression
        # (the other side did not exercise it, not "0 ms") - flag it, don't rank a
        # bogus delta.
        section_deltas.append(
            {
                "section": name,
                "a_heat": value_a,
                "b_heat": value_b,
                "delta_b_minus_a": round(difference, 3),
                "better": _winner(difference) if in_a and in_b else "not_comparable",
            }
        )
    section_deltas.sort(key=lambda d: abs(float(d["delta_b_minus_a"])), reverse=True)

    attribution_deltas: list[dict[str, Any]] = []
    attr_a, attr_b = _attribution_totals(a), _attribution_totals(b)
    for name in sorted(set(attr_a) | set(attr_b)):
        value_a, value_b = attr_a.get(name, 0.0), attr_b.get(name, 0.0)
        difference = value_b - value_a
        attribution_deltas.append(
            {
                "subsystem": name,
                "a_ms": value_a,
                "b_ms": value_b,
                "delta_b_minus_a": round(difference, 1),
                "better": _winner(difference),
            }
        )
    attribution_deltas.sort(key=lambda d: abs(float(d["delta_b_minus_a"])), reverse=True)

    sum_a, sum_b = sum(layers_a.values()), sum(layers_b.values())
    overall = _winner(sum_b - sum_a)

    def late_ticks(summary: dict[str, Any]) -> int:
        return int(((summary.get("metadata") or {}).get("frame") or {}).get("lateTicks") or 0)

    def rate(summary: dict[str, Any], block: str, field: str) -> float:
        return float(((summary.get("metadata") or {}).get(block) or {}).get(field) or 0)

    def layer_signal(summary: dict[str, Any], layer: str, field: str) -> float:
        for entry in summary.get("layers") or []:
            if entry.get("layer") == layer:
                return float((entry.get("signals") or {}).get(field) or 0)
        return 0.0

    late_a, late_b = late_ticks(summary_a), late_ticks(summary_b)
    stw_a = layer_signal(summary_a, "runtime_gc", "stw_pause_worst_ms")
    stw_b = layer_signal(summary_b, "runtime_gc", "stw_pause_worst_ms")
    # Gross GC_malloc churn drives the STW pauses; net heap growth reads ~0.
    alloc_a = rate(summary_a, "gc", "grossAllocMBPerSecond")
    alloc_b = rate(summary_b, "gc", "grossAllocMBPerSecond")
    chunk_a = rate(summary_a, "transfers", "mb_per_second")
    chunk_b = rate(summary_b, "transfers", "mb_per_second")
    return {
        "schema": "7dtd.apm.compare.v2",
        "a": str(a),
        "b": str(b),
        "late_ticks_a": late_a,
        "late_ticks_b": late_b,
        "late_ticks_delta": late_b - late_a,
        "alloc_mb_s_a": round(alloc_a, 2),
        "alloc_mb_s_b": round(alloc_b, 2),
        "stw_worst_ms_a": round(stw_a, 1),
        "stw_worst_ms_b": round(stw_b, 1),
        "chunk_mb_s_a": round(chunk_a, 2),
        "chunk_mb_s_b": round(chunk_b, 2),
        "overall_better": overall,
        "sum_layer_score_a": round(sum_a, 3),
        "sum_layer_score_b": round(sum_b, 3),
        "layers_where_a_better": better_a,
        "layers_where_b_better": better_b,
        "layer_deltas": layer_deltas,
        "section_deltas": section_deltas,
        "attribution_deltas": attribution_deltas,
        "flame_frame_deltas": load_flame_deltas(a, b),
        "note": "Lower layer score and lower per-call section heat are better (less pressure).",
    }


def format_report(cmp: dict[str, Any]) -> str:
    lines = [
        "# APM session compare",
        "",
        f"- **A:** `{cmp['a']}`",
        f"- **B:** `{cmp['b']}`",
        f"- **Overall better (lower total layer pressure):** **{cmp['overall_better']}**",
        f"- Sum layer scores: A={cmp['sum_layer_score_a']}  B={cmp['sum_layer_score_b']}",
        f"- Layers A better: {cmp['layers_where_a_better']}  B better: {cmp['layers_where_b_better']}",
        "",
        "## Layer deltas (B − A; negative = B improved)",
        "",
        "| layer | A | B | Δ | better |",
        "|-------|---|---|---|--------|",
    ]
    for d in cmp["layer_deltas"]:
        lines.append(
            f"| {d['layer']} | {d['a_score']} | {d['b_score']} | {d['delta_b_minus_a']:+} | {d['better']} |"
        )
    if cmp.get("section_deltas"):
        lines += [
            "",
            "## Managed section per-call heat deltas",
            "",
            "| section | A | B | Δ | better |",
            "|---------|---|---|---|--------|",
        ]
        for d in cmp["section_deltas"][:20]:
            lines.append(
                f"| {d['section']} | {d['a_heat']} | {d['b_heat']} | {d['delta_b_minus_a']:+} | {d['better']} |"
            )
    else:
        lines += ["", "## Managed section heat deltas", "", "_No section heat in either session._"]

    lines += [
        "",
        f"- **Late ticks (bridge tick overage >60ms):** A={cmp.get('late_ticks_a')} "
        f"B={cmp.get('late_ticks_b')} (Δ {cmp.get('late_ticks_delta'):+})",
        f"- **Gross GC alloc MB/s (churn):** A={cmp.get('alloc_mb_s_a')} "
        f"B={cmp.get('alloc_mb_s_b')}",
        f"- **Worst STW pause ms (main-thread freeze):** A={cmp.get('stw_worst_ms_a')} "
        f"B={cmp.get('stw_worst_ms_b')}",
        f"- **Chunk stream MB/s:** A={cmp.get('chunk_mb_s_a')} B={cmp.get('chunk_mb_s_b')}",
    ]
    if cmp.get("attribution_deltas"):
        lines += [
            "",
            "## Subsystem attribution deltas (window-scoped ms; needs --reset-bridge on both)",
            "",
            "| subsystem | A ms | B ms | Δ | better |",
            "|-----------|------|------|---|--------|",
        ]
        for d in cmp["attribution_deltas"]:
            lines.append(
                f"| {d['subsystem']} | {d['a_ms']} | {d['b_ms']} | {d['delta_b_minus_a']:+} | {d['better']} |"
            )

    if cmp.get("flame_frame_deltas"):
        lines += [
            "",
            "## Speedscope / folded frame deltas (B − A sample weight)",
            "",
            "| Δ | A | B | frame |",
            "|---|---|---|-------|",
        ]
        for d in cmp["flame_frame_deltas"][:20]:
            frame = str(d.get("frame") or "")[:80]
            lines.append(f"| {d['delta']:+} | {d['a']} | {d['b']} | `{frame}` |")
    else:
        lines += [
            "",
            "## Speedscope / folded frame deltas",
            "",
            "_No stacks.folded in one or both sessions._",
        ]
    return "\n".join(lines) + "\n"


def run_compare(a: Path, b: Path, output_dir: Path | None = None) -> dict[str, Any]:
    """Compare and persist compare.json/compare.md (raises ValueError if incompatible)."""
    result = compare_sessions(a, b)
    text = format_report(result)
    print(text)
    out_dir = output_dir or b
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(out_dir / "compare.json", result)
    atomic_text(out_dir / "compare.md", text)
    return result
