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
from ..models import as_number, layer_signals
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
        gc_layer = layer_signals(summary, "runtime_gc")
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
                # Scores come from unvalidated session JSON (imported bundles,
                # hand edits): a non-numeric value must drop out of the sum,
                # not poison the whole index scan with a float() ValueError.
                "sum_pressure": round(
                    sum(
                        score
                        for score in (as_number(v) for v in layers.values())
                        if score is not None
                    ),
                    2,
                ),
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
        name = _cell(row["dir"])
        # Link the session name only when at least one rendered page exists;
        # a bare directory listing (or a 404) is a dead end for the reader.
        if row.get("has_dashboard") or row.get("has_report"):
            link = (
                f"{row['dir']}/dashboard.html"
                if row.get("has_dashboard")
                else f"{row['dir']}/report.html"
            )
            safe_link = html.escape(link, quote=True)
            name = f'<a href="{safe_link}">{name}</a>'
        stw = row.get("stw_worst_ms")
        flame_icon = ""
        if row.get("has_flame"):
            flame_icon = (
                f'<a class="artifact" href="{html.escape(row["dir"], quote=True)}/cpu/perf/flame.html">'
                '<span role="img" aria-label="flamegraph report available (open it)">\U0001f525</span></a>'
            )
        bridge_icon = ""
        if row.get("has_bridge"):
            bridge_icon = (
                f'<a class="artifact" href="{html.escape(row["dir"], quote=True)}/csharp_bridge.md">'
                '<span role="img" aria-label="bridge capture available (open it)">\U0001f309</span></a>'
            )
        body.append(
            f"<tr>"
            f"<td>{name}</td>"
            f"<td>{_cell(row.get('utc'))}</td>"
            f"<td>{_cell(row.get('pid'))}</td>"
            f"<td>{_cell(row.get('entities'))}/{_cell(row.get('players'))}</td>"
            f"<td>{_cell(row.get('health'), '?')}</td>"
            f"<td>{_cell(row.get('grade'))}</td>"
            f"<td>{_cell(row.get('verdict'))}</td>"
            f"<td>{_cell(row.get('profile'))}</td>"
            f"<td>{_cell(row.get('gross_alloc_mb_s'))}"
            f"{(' / ' + _cell(stw) + 'ms STW') if stw else ''}</td>"
            f"<td>{flame_icon}{bridge_icon}</td>"
            f"</tr>"
        )
    if not rows:
        body.append(
            '<tr><td colspan="10">No sessions yet. Capture one with '
            "<code>uv run 7dtd-server-apm capture --seconds 45 --only all</code>, "
            "then reload this page.</td></tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>7dtd APM sessions</title>
<style>
/* Shared APM web tokens (report + dashboard + session index):
   bg #0f1115 · surface #161a22 · outline #2a2f3a · rule #303642 ·
   text #e8eaed · muted #9aa0a6 · link #8ab4f8 · accent #e6bd3a */
body{{font-family:system-ui;background:#0f1115;color:#e8eaed;margin:24px}}
a{{color:#8ab4f8}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #303642;padding:8px;text-align:left}}
th{{background:#161a22}}
.sr-only{{position:absolute;width:1px;height:1px;margin:-1px;padding:0;border:0;clip-path:inset(50%);overflow:hidden;white-space:nowrap}}
/* Icon-only artifact links are small glyphs; pad them to a 24x24 target
   (WCAG 2.5.8 Target Size Minimum). */
td a.artifact{{display:inline-block;min-width:24px;min-height:24px;line-height:24px;text-align:center;text-decoration:none;font-size:16px}}
td a.artifact:focus-visible{{outline:2px solid #8ab4f8;outline-offset:1px}}
</style></head><body>
<main>
<h1>APM session index</h1>
<p>{len(rows)} sessions</p>
<table>
<caption class="sr-only">APM sessions</caption>
<tr><th scope="col">session</th><th scope="col">utc</th><th scope="col">pid</th><th scope="col">entities/players</th><th scope="col">health</th><th scope="col">grade</th><th scope="col">lag diagnosis</th><th scope="col">profile</th><th scope="col">gross alloc / STW</th><th scope="col">artifacts</th></tr>
{"".join(body)}
</table>
</main>
</body></html>
"""


def write_index(root: Path | None = None) -> int:
    target = root or apm_root()
    rows = scan(target)
    target.mkdir(parents=True, exist_ok=True)
    atomic_json(target / "index.json", {"sessions": rows})
    atomic_text(target / "index.html", html_index(rows))
    return len(rows)
