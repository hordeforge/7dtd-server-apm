"""Map host/native APM evidence → C# systems → mod API fixes.

Bridges weighted CPU frames, structured layer signals, and managed bridge
sections to actionable Harmony targets and EfficientServer / serverconfig
levers. Every emitted bridge separates raw evidence, derived metrics, the
inference drawn from them, and a suggested experiment to confirm the fix.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..io import atomic_json, atomic_text, load_json
from ..models import first_number, first_present
from .catalog import RULES, SECTION_TO_CSHARP
from .flame_delta import load_weights

# Thresholds: evidence below these never fires a rule (no word-matching noise).
NATIVE_SHARE_MIN = 0.005  # >= 0.5% of total sampled frame weight
SECTION_SCORE_MIN = 1.0  # >= 1 ms per call (p95 preferred)

# rule id -> (layer, ((signal name, minimum), ...)); any signal at/over its
# minimum counts as structured evidence for the rule.
SIGNAL_GATES: dict[str, tuple[str, tuple[tuple[str, float], ...]]] = {
    "gc_mono": ("runtime_gc", (("slow_gc_lines", 1), ("collect_a_little_hits", 50))),
    "futex_lock": ("sync_locks", (("slow_futex_lines", 1),)),
    "blocking_io": ("io", (("slow_block_lines", 1),)),
}
# A gated rule also requires its gate's layer to be collected this session.
REQUIRED_LAYER = {rule_id: gate[0] for rule_id, gate in SIGNAL_GATES.items()}

# Subsystem attribution: section prefix -> bucket. Answers "where does the
# frame budget go" (optimizer experiment 1). Deep sections are scaled by the
# bridge's deepSampleRate before summing.
# Additive buckets use the outermost, non-sampled section of each call chain
# so nested inclusive times are not double counted; the sampled inner chain
# (TickEntity > OnUpdateEntity > OnUpdateLive > updateTasks > MoveHelper/paths)
# is reported separately as a drill-down.
SUBSYSTEMS: dict[str, tuple[str, ...]] = {
    "entity_tick": (
        "World.TickEntities",
        "World.TickEntitiesSlice",
        "World.TickEntitiesFlush",
        "World.EntityActivityUpdate",
    ),
    "spawn_director": ("SpawnManagerBiomes.", "AIDirector.", "AIDirectorBloodMoonComponent."),
    "mesh": ("DynamicMeshManager.", "DynamicMeshServer."),
    "deco_world": (
        "DecoManager.",
        "WaterSplashCubes.",
        "WorldBlockTicker.",
        "World.TickSleeperVolumes",
    ),
    "falling_blocks": (
        "World.AddFallingBlock",
        "World.LetBlocksFall",
        "World.GroupFallingBlocks",
        "TrajectorySimulation.",
    ),
    "explosions": (
        "GameManager.ExplosionServer",
        "GameManager.explode",
        "GameManager.ExplodeGroupFrameUpdate",
    ),
    "network": (
        "NetConnectionSimple.FlushSendQueue",
        "NetPackageChunk.",
        "NetPackageMapChunks.",
        "NetPackageWorldFolder.",
        "NetEntityDistribution.",
        "ConnectionManager.",
    ),
    "io_saves": (
        "GameManager.Save",
        "World.SaveWorldState",
        "ChunkProviderGenerateWorld.",
        "ChunkManager.",
    ),
    "managers": (
        "VehicleManager.",
        "DroneManager.",
        "PowerManager.",
        "GameEventManager.",
        "QuestEventManager.",
        "NavObjectManager.",
        "FactionManager.",
        "TurretTracker.",
        "ThreadManager.",
    ),
    "frame_core": ("GameManager.UpdateTick", "World.OnUpdateTick", "GmUpdate"),
}
# Long-lived task sections measure task lifetime, not per-frame CPU; keep them
# out of the attribution denominator.
LONG_RUNNING_MS = 5000.0

# Fallback for snapshots from bridges that predate the per-section deep flag:
# these hooks are registered deep (sampled) by BridgeMod.PatchSections.
DEEP_SECTION_NAMES = frozenset(
    {
        "EntityAlive.updateTasks",
        "EAIManager.Update",
        "PathNavigate.UpdateNavigation",
        "ASPPathNavigate.UpdateNavigation",
        "EntityMoveHelper.UpdateMoveHelper",
        "EntityAlive.FindPath",
        "NetEntityDistributionEntry.updatePlayerList",
        "World.TickEntity",
        "Entity.OnUpdateEntity",
        "EntityAlive.OnUpdateLive",
        "ASPPathNavigate.GetPathTo",
        "ASPPathFinder.Calculate",
        "ASPPathNavigate.pathFollow",
        "EAIApproachAndAttackTarget.Update",
    }
)


# Sampled inner entity chain: nested inclusive times, ranked against each
# other but never summed into the additive denominator.
DRILLDOWN: dict[str, tuple[str, ...]] = {
    "tick_entity": ("World.TickEntity",),
    "on_update_entity": ("Entity.OnUpdateEntity",),
    "on_update_live": ("EntityAlive.OnUpdateLive",),
    "ai_decide": ("EntityAlive.updateTasks", "EAIManager.", "EAIApproachAndAttackTarget."),
    "movement": ("EntityMoveHelper.", "ASPPathNavigate.pathFollow"),
    "pathfinding": (
        "EntityAlive.FindPath",
        "ASPPathNavigate.GetPathTo",
        "ASPPathFinder.",
        "AstarManager.",
    ),
    "net_interest": ("NetEntityDistributionEntry.updatePlayerList",),
}


def _matches(name: str, pattern: str) -> bool:
    """Exact section name, or prefix when the pattern ends with a dot."""
    return name == pattern or (pattern.endswith(".") and name.startswith(pattern))


def attribute_subsystems(
    sections: list[dict[str, Any]],
    deep_sample_rate: int,
    window_updates: int = 0,
    entities: int = 0,
) -> dict[str, Any]:
    totals: dict[str, float] = dict.fromkeys(SUBSYSTEMS, 0.0)
    drilldown_totals: dict[str, float] = dict.fromkeys(DRILLDOWN, 0.0)
    details: dict[str, list[dict[str, Any]]] = {key: [] for key in SUBSYSTEMS}
    drilldown_details: dict[str, list[dict[str, Any]]] = {key: [] for key in DRILLDOWN}
    long_running: list[str] = []
    unmapped: list[str] = []
    for section in sections:
        name = str(section.get("name") or "")
        avg = float(section.get("avgMs") or 0)
        if avg >= LONG_RUNNING_MS:
            long_running.append(name)
            continue
        is_deep = bool(section.get("deep")) or (
            "deep" not in section and name in DEEP_SECTION_NAMES
        )
        scale = deep_sample_rate if is_deep else 1
        scaled_ms = float(section.get("totalMs") or 0) * scale
        entry = {
            "name": name,
            "scaled_total_ms": round(scaled_ms, 1),
            "calls_scaled": int(section.get("calls") or 0) * scale,
            "deep_sampled": is_deep,
        }
        for drill, drill_prefixes in DRILLDOWN.items():
            if any(_matches(name, prefix) for prefix in drill_prefixes):
                drilldown_totals[drill] += scaled_ms
                drilldown_details[drill].append(entry)
                break
        else:
            for subsystem, prefixes in SUBSYSTEMS.items():
                if any(_matches(name, prefix) for prefix in prefixes):
                    totals[subsystem] += scaled_ms
                    details[subsystem].append(entry)
                    break
            else:
                if scaled_ms > 0:
                    unmapped.append(name)
    # Sections nest: frame_core (GameManager.UpdateTick) includes most other
    # buckets. Approximate its exclusive time by subtracting the buckets that
    # run inside the tick on the main thread (mesh + connection managers are
    # peer MonoBehaviours; network serialization is partly worker-thread).
    nested = (
        "entity_tick",
        "spawn_director",
        "deco_world",
        "falling_blocks",
        "explosions",
        "io_saves",
    )
    frame_core_exclusive = max(
        0.0, totals.get("frame_core", 0.0) - sum(totals[name] for name in nested)
    )
    # Use frame_core's EXCLUSIVE time in the totals so the share denominator does
    # not double-count the nested buckets (which are already counted on their own
    # line). Without this, every subsystem share is deflated and they do not sum
    # to ~100%.
    if "frame_core" in totals:
        totals["frame_core"] = frame_core_exclusive

    def scaled_calls(section_name: str) -> int:
        for bucket in (details, drilldown_details):
            for entries in bucket.values():
                for entry in entries:
                    if entry["name"] == section_name:
                        return int(entry["calls_scaled"])
        return 0

    enqueued = scaled_calls("EntityAlive.FindPath")
    drained = scaled_calls("ASPPathNavigate.GetPathTo")
    path_pipeline = {
        "enqueued": enqueued,
        "drained": drained,
        "computed": scaled_calls("ASPPathFinder.Calculate"),
        "backlog_estimate": enqueued - drained,
        "note": "deep-sampled counts scaled by rate; noisy below ~50 real calls",
    }

    denominator = sum(totals.values())
    ranked = [
        {
            "subsystem": subsystem,
            "scaled_total_ms": round(total, 1),
            "share": round(total / denominator, 4) if denominator else 0.0,
            "sections": sorted(details[subsystem], key=lambda s: -float(s["scaled_total_ms"])),
        }
        for subsystem, total in sorted(totals.items(), key=lambda kv: -kv[1])
        if total > 0
    ]
    return {
        "note": (
            "scaled_total_ms sums section totalMs (deep sections scaled by "
            f"deepSampleRate={deep_sample_rate}); shares are of instrumented, "
            "non-long-running managed time only"
        ),
        "deep_sample_rate": deep_sample_rate,
        "measured_ms": round(denominator, 1),
        "ms_per_tick": round(denominator / window_updates, 3) if window_updates else None,
        "ms_per_entity_tick": (
            round(denominator / window_updates / entities, 5)
            if window_updates and entities
            else None
        ),
        "window_updates": window_updates or None,
        "entities": entities or None,
        "frame_core_exclusive_ms": round(frame_core_exclusive, 1),
        "path_pipeline": path_pipeline,
        "subsystems": ranked,
        "entity_drilldown": {
            "note": (
                "nested inclusive chain (TickEntity > OnUpdateEntity > OnUpdateLive > "
                "updateTasks > movement/paths), deep-sampled and scaled; rank within "
                "this list, never add to subsystem shares"
            ),
            "levels": [
                {
                    "level": level,
                    "scaled_total_ms": round(total, 1),
                    "sections": sorted(
                        drilldown_details[level],
                        key=lambda s: -float(s["scaled_total_ms"]),
                    ),
                }
                for level, total in drilldown_totals.items()
                if total > 0
            ],
        },
        "long_running_excluded": long_running,
        "unmapped_sections": unmapped,
    }


def attribute_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Subsystem attribution from an already-parsed bridge snapshot document.

    Raises on malformed nested values (a non-object "measurement"/"update"/
    "world" block); callers decide whether that poisons the whole stage.
    """
    return attribute_subsystems(
        doc.get("sections") or [],
        int((doc.get("measurement") or {}).get("deepSampleRate") or 1),
        window_updates=int((doc.get("update") or {}).get("windowUpdates") or 0),
        entities=int((doc.get("world") or {}).get("entities") or 0),
    )


def attribute_snapshot(session: Path) -> dict[str, Any] | None:
    """Subsystem attribution computed fresh from the session's bridge snapshot.

    Shared by the finalize-time lag diagnosis and this analyzer so both apply
    identical deep-sample scaling. Returns None when the snapshot is missing or
    malformed: attribution is best-effort enrichment, never a failed stage.
    """
    path = session / "app/apm_app.json"
    if not path.is_file():
        return None
    try:
        return attribute_document(json.loads(path.read_text(encoding="utf-8")))
    except (AttributeError, TypeError, ValueError, OSError):
        # AttributeError: a "measurement"/"update"/"world" block that parsed as
        # a non-object (list/str) has no .get; treat the whole document as bad.
        return None


def ranked_section_heats(session: Path) -> dict[str, float | None]:
    """name -> per-call heat (ms) from csharp_bridge.json's ranked sections.

    Single parser for the budget gate and session compare so their field
    fallback chains cannot drift (they had: one fell back score->p95->avgMs,
    the other score->avgMs). `score` is what section_rank wrote (p95 preferred,
    else avgMs), so the raw p95/avgMs keys only matter for hand-made documents;
    a legitimate 0 stays 0 instead of falling through to the next field.
    The value is None when an entry carries no parseable duration - UNKNOWN,
    never a healthy zero; callers decide whether None skips (budget gate) or
    reads as a present-at-0 tie (compare). Raises ValueError naming the path
    when the JSON is unreadable; callers surface that, never gate on silence.
    """
    path = session / "csharp_bridge.json"
    if not path.is_file():
        return {}
    try:
        data = load_json(path)
    except json.JSONDecodeError as error:
        raise ValueError(f"cannot parse {path}: {error}") from None
    heats: dict[str, float | None] = {}
    for section in data.get("top_managed_sections") or []:
        name = str(section.get("name") or "")
        if not name:
            continue
        heats[name] = first_number(section.get("score"), section.get("p95"), section.get("avgMs"))
    return heats


def load_folded_frames(session: Path) -> list[tuple[str, int]]:
    """Return (frame_name, weight) from folded stacks (weight per frame occurrence)."""
    folded = session / "cpu/perf/stacks.folded"
    if not folded.exists():
        return []
    weights = load_weights(folded)
    return sorted(weights.items(), key=lambda kv: -kv[1])


def load_speedscope_frames(session: Path) -> list[tuple[str, int]]:
    path = session / "cpu/perf/profile.speedscope.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    frames = data.get("shared", {}).get("frames") or []
    names = [str(frame.get("name", "")) for frame in frames]
    counts: dict[str, int] = defaultdict(int)
    for profile in data.get("profiles") or []:
        samples = profile.get("samples") or []
        weights = profile.get("weights") or [1] * len(samples)
        for sample, weight in zip(samples, weights, strict=False):
            # json.loads accepts Infinity/NaN literals; int(inf) would raise
            # OverflowError and crash the whole analysis. Skip non-finite weights.
            try:
                count = float(weight)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(count):
                continue
            for index in sample:
                if 0 <= index < len(names):
                    counts[names[index]] += int(count)
    return sorted(counts.items(), key=lambda kv: -kv[1])


def parse_managed_sections(session: Path, extra: Path | None) -> list[dict[str, Any]]:
    """Extract section stats from managed bridge JSON dumps or app scrapes."""
    sections: list[dict[str, Any]] = []

    def ingest_obj(obj: dict[str, Any]) -> None:
        for section in obj.get("sections") or []:
            if isinstance(section, dict) and section.get("name") and section not in sections:
                sections.append(section)

    def ingest_file(path: Path) -> None:
        with contextlib.suppress(json.JSONDecodeError):
            ingest_obj(json.loads(path.read_text(encoding="utf-8")))

    named = [
        path
        for path in (extra, session / "app/managed_sections.json", session / "app/apm_app.json")
        if path and Path(path).is_file()
    ]
    # Ingest each distinct file once; the *.json sweep below must not re-parse
    # the ones already read by name.
    ingested: set[Path] = {Path(path).resolve() for path in named}
    for path in named:
        ingest_file(Path(path))

    scrape = session / "app/bridge.jsonl"
    if scrape.exists():
        for line in scrape.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            sections.extend(parse_section_line(str(record.get("text") or "")))

    excerpt = session / "app/efficientserver_log_excerpt.txt"
    if excerpt.exists():
        sections.extend(parse_section_line(excerpt.read_text(encoding="utf-8", errors="replace")))

    app_dir = session / "app"
    if app_dir.is_dir():
        for path in sorted(app_dir.glob("*.json")):
            if path.resolve() in ingested:
                continue
            with contextlib.suppress(json.JSONDecodeError, OSError):
                ingest_obj(json.loads(path.read_text(encoding="utf-8")))

    return sections


def parse_section_line(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for match in re.finditer(r"([A-Za-z0-9_.]+)=([\d.]+)ms\(x(\d+),max=([\d.]+)\)", text):
        out.append(
            {
                "name": match.group(1),
                "avgMs": float(match.group(2)),
                "calls": int(match.group(3)),
                "maxMs": float(match.group(4)),
            }
        )
    frame_avg = re.search(r"avg=([\d.]+)ms", text)
    if frame_avg and not out:
        avg = float(frame_avg.group(1))
        out.append({"name": "GmUpdate", "avgMs": avg, "calls": 1, "maxMs": avg})
    return out


def section_rank(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and rank comparable per-call duration, never cumulative capture time."""
    scored = []
    for section in sections:
        name = str(section.get("name") or "")
        # Section dicts also come from imported bundles' app/*.json sweeps
        # (unvalidated): a truthy non-numeric field ("avgMs": [5]) must degrade
        # to 0 like compare.load_sections' present-at-0 tie, never raise
        # TypeError/ValueError out of the ranking. A legitimate 0 stays 0.
        avg = first_present(section.get("avgMs"))
        calls = int(first_present(section.get("calls")))
        total = first_present(section.get("totalMs"), avg * calls)
        p95 = first_present(section.get("p95Ms"), section.get("p95"))
        scored.append(
            {
                "name": name,
                "avgMs": avg,
                "calls": calls,
                "totalMs": total,
                "p95": p95,
                "score": p95 if p95 > 0 else avg,
                "unit": "ms_per_call",
            }
        )
    best: dict[str, dict[str, Any]] = {}
    for section in scored:
        name = str(section["name"])
        if name not in best or section["score"] > best[name]["score"]:
            best[name] = section
    return sorted(best.values(), key=lambda x: -float(x["score"]))


def layer_state(summary: dict[str, Any]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    collected: set[str] = set()
    signals: dict[str, dict[str, Any]] = {}
    for layer in summary.get("layers") or []:
        name = str(layer.get("layer"))
        if layer.get("state") == "collected":
            collected.add(name)
            signals[name] = dict(layer.get("signals") or {})
    return collected, signals


def _native_evidence(
    rule: dict[str, Any], frames: list[tuple[str, int]], total_weight: int
) -> tuple[list[dict[str, Any]], float]:
    patterns = rule.get("native_any") or []
    if not patterns or not total_weight:
        return [], 0.0
    matched: list[dict[str, Any]] = [
        {"frame": name, "weight": weight}
        for name, weight in frames
        if any(pattern in name for pattern in patterns)
    ]
    share = sum(int(m["weight"]) for m in matched) / total_weight
    return matched[:10], share


def _signal_evidence(rule_id: str, signals: dict[str, dict[str, Any]]) -> dict[str, float]:
    gate = SIGNAL_GATES.get(rule_id)
    if not gate:
        return {}
    layer, minimums = gate
    layer_signals = signals.get(layer) or {}
    hits: dict[str, float] = {}
    for name, minimum in minimums:
        value = layer_signals.get(name)
        if isinstance(value, (int, float)) and value >= minimum:
            hits[name] = float(value)
    return hits


def _section_scores(top_sections: list[dict[str, Any]]) -> dict[str, float]:
    """Index sections by full name AND bare method suffix.

    The bridge registers "World.TickEntities" while catalog rules and legacy
    log scrapes use "TickEntities"; both spellings must match.
    """
    index: dict[str, float] = {}
    for section in top_sections:
        name = str(section["name"])
        for key in {name, name.rsplit(".", 1)[-1]}:
            index[key] = max(index.get(key, 0.0), float(section["score"]))
    return index


def match_rules(
    frames: list[tuple[str, int]],
    top_sections: list[dict[str, Any]],
    collected_layers: set[str],
    layer_signals: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    total_weight = sum(weight for _, weight in frames)
    section_score = _section_scores(top_sections)
    hits = []

    for rule in RULES:
        required = REQUIRED_LAYER.get(str(rule["id"]))
        if required and required not in collected_layers:
            continue

        native_frames, native_share = _native_evidence(rule, frames, total_weight)
        signal_hits = _signal_evidence(str(rule["id"]), layer_signals)

        def score_of(token: str, index: dict[str, float] = section_score) -> float:
            direct = index.get(token)
            if direct is not None:
                return direct
            return index.get(token.rsplit(".", 1)[-1], 0.0)

        sec_hits = [
            s for s in (rule.get("prof_sections") or []) if score_of(s) >= SECTION_SCORE_MIN
        ]

        native_ok = native_share >= NATIVE_SHARE_MIN
        if not (native_ok or signal_hits or sec_hits):
            continue

        confidence = 0.0
        if native_ok:
            confidence += 0.45 + min(0.2, native_share * 2)
        if signal_hits:
            confidence += 0.3
        if sec_hits:
            confidence += 0.35
        confidence = min(1.0, confidence)

        section_heat = sum(score_of(s) for s in sec_hits)
        heat = section_heat + native_share * 100 + (15 if signal_hits else 0)

        csharp_extra = [
            f"{s} → {', '.join(meta.get('types') or [])}.{'/'.join(meta.get('methods') or [])}"
            for s in sec_hits
            if (meta := SECTION_TO_CSHARP.get(s) or SECTION_TO_CSHARP.get(s.rsplit(".", 1)[-1]))
        ]
        hits.append(
            {
                "id": rule["id"],
                "title": rule["title"],
                "evidence": {
                    "native_frames": native_frames if native_ok else [],
                    "layer_signals": signal_hits,
                    "managed_sections": [
                        {"name": s, "score_ms_per_call": score_of(s)} for s in sec_hits
                    ],
                },
                "derived": {
                    "native_weight_share": round(native_share, 4),
                    "section_heat_ms": round(section_heat, 2),
                    "thresholds": {
                        "native_share_min": NATIVE_SHARE_MIN,
                        "section_score_min_ms": SECTION_SCORE_MIN,
                    },
                },
                "inference": {
                    "confidence": round(confidence, 2),
                    "priority": round(heat * confidence, 2),
                    "required_layer": required,
                    "csharp": (rule.get("csharp") or []) + csharp_extra,
                },
                "experiment": {
                    "suggestion": (
                        "Apply one lever below, re-run the identical "
                        "`7dtd-server-apm scenario run` workload, then `7dtd-server-apm compare` "
                        "the before/after sessions."
                    ),
                    "harmony_targets": rule.get("harmony_targets") or [],
                    "mod_apis": rule.get("mod_apis") or [],
                },
            }
        )

    hits.sort(key=lambda hit: -float(hit["inference"]["priority"]))
    return hits


def build_playbook(hits: list[dict[str, Any]], top_sections: list[dict[str, Any]]) -> list[str]:
    lines = []
    if not hits:
        lines.append(
            "No evidence cleared the bridge thresholds. Install the standalone APM bridge, run "
            "`APM bridge DeepMode` + load (`7dtd-server-apm scenario run`), re-capture, re-run this tool."
        )
        if top_sections:
            lines.append(
                "APM bridge managed sections without native match: "
                + ", ".join(f"{s['name']}({s['score']:.1f})" for s in top_sections[:5])
            )
        return lines

    lines.append("## Recommended order of work")
    for i, hit in enumerate(hits[:6], 1):
        inference = hit["inference"]
        evidence = hit["evidence"]
        lines.append(
            f"\n### {i}. {hit['title']} "
            f"(priority={inference['priority']}, conf={inference['confidence']})"
        )
        if evidence["native_frames"]:
            share = hit["derived"]["native_weight_share"]
            lines.append(
                f"Native evidence ({share:.1%} of sampled weight): "
                + ", ".join(str(f["frame"]) for f in evidence["native_frames"][:4])
            )
        if evidence["layer_signals"]:
            lines.append(
                "Structured layer signals: "
                + ", ".join(f"{k}={v}" for k, v in evidence["layer_signals"].items())
            )
        if evidence["managed_sections"]:
            lines.append(
                "Managed bridge sections: "
                + ", ".join(
                    f"{s['name']}({s['score_ms_per_call']:.1f}ms)"
                    for s in evidence["managed_sections"]
                )
            )
        lines.append("C# surface:")
        for surface in inference["csharp"]:
            lines.append(f"  - {surface}")
        lines.append("Harmony targets (start here):")
        for target in hit["experiment"]["harmony_targets"]:
            lines.append(f"  - `{target}`")
        lines.append("Mod / config levers:")
        for lever in hit["experiment"]["mod_apis"]:
            lines.append(f"  - {lever}")
        lines.append(f"Experiment: {hit['experiment']['suggestion']}")
    return lines


def stall_correlation(session: Path) -> list[dict[str, Any]]:
    """For each frame spike, what else happened within +/-2s (cause evidence)."""
    events_path = session / "events.json"
    if not events_path.is_file():
        return []
    try:
        events = load_json(events_path).get("events") or []
    except (ValueError, OSError):
        return []
    timed = [e for e in events if isinstance(e.get("t"), (int, float))]
    spikes = sorted(
        (e for e in timed if e.get("kind") == "frame_spike"),
        key=lambda e: -float(e.get("value") or 0),
    )[:10]
    report = []
    for spike in spikes:
        center = float(spike["t"])
        nearby: dict[str, int] = {}
        for event in timed:
            if event is spike or abs(float(event["t"]) - center) > 2.0:
                continue
            kind = str(event.get("kind"))
            nearby[kind] = nearby.get(kind, 0) + 1
        # GC SLOW lines from bpftrace are untimed, so match by duration: a GC
        # pause close in magnitude to the frame spike is almost certainly its
        # cause (Mono stop-the-world blocks the whole frame).
        spike_ms = float(spike.get("value") or 0)
        gc_ms = 0.0
        # A zero-duration "spike" carries no magnitude to match a GC pause
        # against (the tolerance collapses to exact-zero); skip the correlation.
        if spike_ms > 0:
            for event in events:
                if event.get("kind") != "gc":
                    continue
                match = re.search(r"(\d+)\s*us", str(event.get("message") or ""))
                if not match:
                    continue
                candidate = int(match.group(1)) / 1000
                if abs(candidate - spike_ms) <= spike_ms * 0.5:
                    gc_ms = max(gc_ms, candidate)
        # spike_ms > 0 here too: without it a zero-duration spike gives 0 >= 0 and
        # is falsely blamed on a GC pause (the search loop above already skipped).
        likely = "gc_pause" if spike_ms > 0 and gc_ms >= spike_ms * 0.5 else "unknown"
        report.append(
            {
                "t": center,
                "gm_update_ms": spike.get("value"),
                "message": spike.get("message"),
                "nearby_events": nearby,
                "gc_pause_ms": round(gc_ms, 1) if gc_ms else None,
                "likely_cause": likely,
            }
        )
    return report


def analyze(session: Path, snapshot: Path | None = None) -> dict[str, Any]:
    """Correlate all evidence and write csharp_bridge.json/.md."""
    frames = load_folded_frames(session) or load_speedscope_frames(session)
    sections = section_rank(parse_managed_sections(session, snapshot))
    summary_path = session / "summary.json"
    summary = load_json(summary_path) if summary_path.is_file() else {}
    collected_layers, layer_signals = layer_state(summary)
    hits = match_rules(frames, sections, collected_layers, layer_signals)
    playbook = build_playbook(hits, sections)

    attribution: dict[str, Any] = attribute_snapshot(session) or {}

    top_frames = frames[:25]
    out: dict[str, Any] = {
        "schema": "7dtd.apm.bridge.v3",
        "session": session.name,
        "attribution": attribution,
        "stall_correlation": stall_correlation(session),
        "top_managed_sections": sections[:15],
        "top_native_frames": [{"frame": name, "weight": weight} for name, weight in top_frames],
        "bridges": hits,
        "playbook_md": "\n".join(playbook),
        "how_to_use": {
            "1": "Read bridges[] ordered by inference.priority",
            "2": "Check evidence/derived to see exactly why each bridge fired",
            "3": "Run the suggested experiment and compare compatible APM sessions",
            "4": "For exact method bodies see 7dtd-engine-research/il/<Type>.txt or ILSpy Assembly-CSharp",
        },
    }
    atomic_json(session / "csharp_bridge.json", out)
    atomic_text(
        session / "csharp_bridge.md",
        "# Native → C# → Mod API bridge\n\n"
        + "\n".join(playbook)
        + "\n\n## Top managed bridge sections\n\n"
        + "\n".join(
            f"- **{s['name']}** score={s['score']:.2f} avgMs={s['avgMs']} calls={s['calls']}"
            for s in sections[:12]
        )
        + "\n\n## Top native frames (folded/speedscope)\n\n"
        + "\n".join(f"- `{name}` ×{weight}" for name, weight in top_frames[:20])
        + "\n",
    )
    return out
