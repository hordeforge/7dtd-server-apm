"""Index all APM sessions under the data root into index.json + index.html.

Health comes solely from each session's health.json; no inline recomputation.
"""

from __future__ import annotations

import contextlib
import html
import json
from pathlib import Path
from typing import Any

from ..io import atomic_json, atomic_text, load_json
from ..paths import apm_root


def scan(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for directory in sorted(root.iterdir(), reverse=True):
        if not directory.is_dir() or not directory.name.startswith("session_"):
            continue
        summary_path = directory / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = load_json(summary_path)
        except (json.JSONDecodeError, ValueError):
            continue
        health: dict[str, Any] = {}
        health_path = directory / "health.json"
        if health_path.is_file():
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                health = load_json(health_path)
        if not health:
            health = summary.get("health") or {}  # sessions finalized before v2.2
        layers = {
            layer["layer"]: layer.get("score")
            for layer in summary.get("layers") or []
            if isinstance(layer, dict) and layer.get("layer")
        }
        meta = summary.get("meta") or {}
        lag = (summary.get("metadata") or {}).get("lag_diagnosis") or {}
        verdict = lag.get("verdict") or ""
        profile = lag.get("profile") or ""
        profile_tag = (
            "spike"
            if "spike-driven" in profile
            else "compute"
            if "compute-bound" in profile
            else ""
        )
        world = (summary.get("metadata") or {}).get("world") or {}
        gc = (summary.get("metadata") or {}).get("gc") or {}
        gc_layer = next(
            (
                layer.get("signals") or {}
                for layer in summary.get("layers") or []
                if layer.get("layer") == "runtime_gc"
            ),
            {},
        )
        rows.append(
            {
                "dir": directory.name,
                "path": str(directory),
                "verdict": verdict,
                "profile": profile_tag,
                "gross_alloc_mb_s": gc.get("grossAllocMBPerSecond"),
                "stw_worst_ms": gc_layer.get("stw_pause_worst_ms"),
                "entities": world.get("entities"),
                "players": world.get("players"),
                "utc": meta.get("utc"),
                "pid": meta.get("pid"),
                "seconds": meta.get("seconds"),
                "health": health.get("health"),
                "grade": health.get("grade"),
                "layers": layers,
                "sum_pressure": round(sum(float(v or 0) for v in layers.values()), 2),
                "has_flame": (directory / "cpu/perf/flame.html").is_file(),
                "has_bridge": (directory / "csharp_bridge.md").is_file(),
                "has_report": (directory / "report.html").is_file(),
                "has_dashboard": (directory / "dashboard.html").is_file(),
            }
        )
    return rows


def _cell(value: Any, blank: str = "") -> str:
    # Every dynamic cell comes from unvalidated session JSON (health.json /
    # summary.json / meta) whose numeric fields are not type-enforced, so a
    # crafted file could smuggle HTML through any of them. Escape all of them.
    return html.escape(str(value), quote=True) if value not in (None, "") else blank


def html_index(rows: list[dict[str, Any]]) -> str:
    body = []
    for row in rows:
        link = (
            f"{row['dir']}/dashboard.html"
            if row.get("has_dashboard")
            else f"{row['dir']}/report.html"
        )
        safe_link = html.escape(link, quote=True)
        stw = row.get("stw_worst_ms")
        body.append(
            f"<tr>"
            f'<td><a href="{safe_link}">{_cell(row["dir"])}</a></td>'
            f"<td>{_cell(row.get('utc'))}</td>"
            f"<td>{_cell(row.get('pid'))}</td>"
            f"<td>{_cell(row.get('entities'))}/{_cell(row.get('players'))}</td>"
            f"<td>{_cell(row.get('health'), '?')}</td>"
            f"<td>{_cell(row.get('grade'))}</td>"
            f"<td>{_cell(row.get('verdict'))}</td>"
            f"<td>{_cell(row.get('profile'))}</td>"
            f"<td>{_cell(row.get('gross_alloc_mb_s'))}"
            f"{(' / ' + _cell(stw) + 'ms STW') if stw else ''}</td>"
            f"<td>{'🔥' if row.get('has_flame') else ''}"
            f"{' 🌉' if row.get('has_bridge') else ''}</td>"
            f"</tr>"
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>7dtd APM sessions</title>
<style>
body{{font-family:system-ui;background:#0f1115;color:#e8eaed;margin:24px}}
a{{color:#8ab4f8}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #333;padding:8px;text-align:left}}
th{{background:#1a1d24}}
</style></head><body>
<h1>APM session index</h1>
<p>{len(rows)} sessions</p>
<table>
<tr><th>session</th><th>utc</th><th>pid</th><th>ent/ply</th><th>health</th><th>grade</th><th>lag diagnosis</th><th>profile</th><th>gross alloc / STW</th><th>artifacts</th></tr>
{"".join(body)}
</table>
</body></html>
"""


def write_index(root: Path | None = None) -> int:
    target = root or apm_root()
    rows = scan(target)
    target.mkdir(parents=True, exist_ok=True)
    atomic_json(target / "index.json", {"sessions": rows})
    atomic_text(target / "index.html", html_index(rows))
    return len(rows)
