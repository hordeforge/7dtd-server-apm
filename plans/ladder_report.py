"""Scale-ladder curve: entities vs per-tick cost vs stalls."""

import json
from pathlib import Path
from typing import Any

root = Path.home() / ".local/share/7dtd-server-apm"
rows = []
for session in sorted(root.glob("session_*")):
    workload = session / "workload.json"
    if not workload.is_file():
        continue
    w = json.loads(workload.read_text(encoding="utf-8"))
    label = w.get("label", "")
    if not label.startswith("ladder-") or label.endswith("invalid"):
        continue
    bridge = json.loads((session / "csharp_bridge.json").read_text(encoding="utf-8"))
    attribution = bridge.get("attribution") or {}
    summary = json.loads((session / "summary.json").read_text(encoding="utf-8"))
    frame = (summary.get("metadata") or {}).get("frame") or {}
    scheduler: dict[Any, Any] = next(
        (L for L in summary.get("layers", []) if L.get("layer") == "scheduler"), {}
    )
    subsystems = {s["subsystem"]: s["scaled_total_ms"] for s in attribution.get("subsystems") or []}
    entities = attribution.get("entities") or 0
    ticks = attribution.get("window_updates") or 0
    entity_ms = subsystems.get("entity_tick", 0.0)
    rows.append(
        {
            "label": label,
            "start": (w.get("workload") or {}).get("zombieAliveAtStart"),
            "end": (w.get("workload") or {}).get("zombieAliveAtEnd"),
            "entities_snapshot": entities,
            "ticks": ticks,
            "tick_avg_ms": frame.get("tickIntervalAvgMs"),
            "gm_avg_ms": frame.get("gmUpdateAvgMs"),
            "late_ticks": frame.get("lateTicks"),
            "entity_tick_ms": entity_ms,
            "entity_ms_per_entity_tick": (
                round(entity_ms / ticks / entities, 4) if ticks and entities else None
            ),
            "stall_ms": (scheduler.get("signals") or {}).get("main_thread_offcpu_ms"),
            "disk_block_ms": (scheduler.get("signals") or {}).get("disk_block_ms"),
            "tick_stall_ms": frame.get("tickStallMsTotal"),
            "gc_spikes": [
                (s.get("gm_update_ms"), s.get("gc_pause_ms"))
                for s in (bridge.get("stall_correlation") or [])
                if s.get("likely_cause") == "gc_pause"
            ],
            "measured_ms": attribution.get("measured_ms"),
            "path": {
                k: v for k, v in (attribution.get("path_pipeline") or {}).items() if k != "note"
            },
        }
    )

for row in sorted(rows, key=lambda r: r["label"]):
    print(
        f"\n=== {row['label']}  zombies start={row['start']} end={row['end']} "
        f"snapshot_entities={row['entities_snapshot']}"
    )
    print(
        f"    tick avg {row['tick_avg_ms']} ms | gmUpdate avg {row['gm_avg_ms']} ms | "
        f"late ticks {row['late_ticks']} | main-thread stall {row['stall_ms']} ms"
    )
    print(
        f"    entity_tick {row['entity_tick_ms']} ms over {row['ticks']} ticks "
        f"-> {row['entity_ms_per_entity_tick']} ms/entity/tick"
    )
    print(
        f"    tick_stall_total {row['tick_stall_ms']} ms | disk_block {row['disk_block_ms']} ms "
        f"| gc-caused spikes {row['gc_spikes']}"
    )
    print(f"    instrumented total {row['measured_ms']} ms | path {row['path']}")
