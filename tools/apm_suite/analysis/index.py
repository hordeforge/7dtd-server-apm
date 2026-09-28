"""Index all APM sessions under the data root into index.json + index.html.

Health comes solely from each session's health.json; no inline recomputation.
"""

from __future__ import annotations

import contextlib
import html
from pathlib import Path
from typing import Any

from ..io import atomic_json, atomic_text, load_json
from ..models import as_mapping, as_number, layer_signals
from ..paths import apm_root
from ..web_tokens import base_css

INDEX_CSS = """
main{max-width:1400px;margin:0 auto}
main>p{margin:.2rem 0 1rem}
/* Artifact links are words, not glyphs: an emoji is a different visual
   language from every other cell here and reads as decoration. */
a.artifact{text-decoration:none;white-space:nowrap}
a.artifact+a.artifact{margin-left:.5rem}
"""


def _text(value: Any) -> str:
    """A JSON string, or "" for every other shape (substring tests need one)."""
    return value if isinstance(value, str) else ""


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
        except (ValueError, OSError):
            # OSError: the session was pruned by a concurrent process between
            # iterdir and this read; skip it like any other unreadable summary.
            continue
        health: dict[str, Any] = {}
        health_path = directory / "health.json"
        if health_path.is_file():
            with contextlib.suppress(ValueError, OSError):
                health = load_json(health_path)
        if not health:
            # sessions finalized before v2.2
            health = as_mapping(summary.get("health"))
        entries = summary.get("layers")
        layers = {
            layer["layer"]: layer.get("score")
            for layer in (entries if isinstance(entries, list) else [])
            if isinstance(layer, dict) and layer.get("layer")
        }
        meta = as_mapping(summary.get("meta"))
        metadata = as_mapping(summary.get("metadata"))
        lag = as_mapping(metadata.get("lag_diagnosis"))
        verdict = _text(lag.get("verdict"))
        profile = _text(lag.get("profile"))
        profile_tag = (
            "spike"
            if "spike-driven" in profile
            else "compute"
            if "compute-bound" in profile
            else ""
        )
        world = as_mapping(metadata.get("world"))
        gc = as_mapping(metadata.get("gc"))
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
        base = html.escape(row["dir"], quote=True)
        flame_link = (
            f'<a class="artifact" href="{base}/cpu/perf/flame.html">flame</a>'
            if row.get("has_flame")
            else ""
        )
        bridge_link = (
            f'<a class="artifact" href="{base}/csharp_bridge.md">bridge</a>'
            if row.get("has_bridge")
            else ""
        )
        body.append(
            f"<tr>"
            f"<td>{name}</td>"
            f"<td>{_cell(row.get('utc'))}</td>"
            f'<td class="num">{_cell(row.get("pid"))}</td>'
            f'<td class="num">{_cell(row.get("entities"))}/{_cell(row.get("players"))}</td>'
            f"<td>{_cell(row.get('health'), '?')}</td>"
            f"<td>{_cell(row.get('grade'))}</td>"
            f"<td>{_cell(row.get('verdict'))}</td>"
            f"<td>{_cell(row.get('profile'))}</td>"
            f'<td class="num">{_cell(row.get("gross_alloc_mb_s"))}'
            f"{(' / ' + _cell(stw) + 'ms STW') if stw else ''}</td>"
            f"<td>{flame_link}{bridge_link}</td>"
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
<style>{base_css(INDEX_CSS)}</style></head><body>
<main>
<h1>APM session index</h1>
<p class="muted">{len(rows)} sessions</p>
<div class="scroll"><table>
<caption class="sr-only">APM sessions</caption>
<tr><th scope="col">session</th><th scope="col">utc</th><th scope="col" class="num">pid</th><th scope="col" class="num">entities/players</th><th scope="col">health</th><th scope="col">grade</th><th scope="col">lag diagnosis</th><th scope="col">profile</th><th scope="col" class="num">gross alloc / STW</th><th scope="col">artifacts</th></tr>
{"".join(body)}
</table></div>
</main>
</body></html>
"""


def write_index(root: Path | None = None) -> int:
    target = root or apm_root()
    rows = scan(target)
    target.mkdir(parents=True, exist_ok=True)
    # Owner-only on shared hosts: index.json carries every session's absolute
    # path and health/grade data, and `index` can be the first command run, so
    # this can be the call that creates the store. Same contract as capture and
    # import, which chmod the root right after creating it.
    with contextlib.suppress(OSError):
        target.chmod(0o700)
    atomic_json(target / "index.json", {"sessions": rows})
    atomic_text(target / "index.html", html_index(rows))
    return len(rows)
