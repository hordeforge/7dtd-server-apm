from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .io import atomic_text, load_json

TEMPLATES = Path(__file__).with_name("templates")
ENV = Environment(
    loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(("html", "xml", "j2"))
)


def _load(path: Path) -> dict[str, Any]:
    """Best-effort JSON load. A malformed artifact must not crash rendering (the
    report is a summary, not a source of truth) - degrade to an empty dict."""
    if not path.is_file():
        return {}
    try:
        return load_json(path)
    except (ValueError, OSError):
        return {}


def render_session(session: Path) -> None:
    summary = _load(session / "summary.json")
    health = _load(session / "health.json") or summary.get("health") or {}
    events_doc = _load(session / "events.json")
    bridge_doc = _load(session / "csharp_bridge.json")
    meta = summary.get("meta") or summary.get("metadata") or _load(session / "meta.json")
    candidates = [
        ("Interactive flame", "cpu/perf/flame.html"),
        ("Speedscope", "cpu/perf/profile.speedscope.json"),
        ("C# bridge", "csharp_bridge.md"),
        ("Budget", "budget_check.txt"),
        ("Events", "events.json"),
    ]
    context = {
        "session": session,
        "summary": summary,
        "meta": meta,
        "health": health,
        "layers": summary.get("layers") or [],
        "events": (events_doc.get("events") or [])[:100],
        "bridges": (bridge_doc.get("bridges") or [])[:20],
        "attribution": bridge_doc.get("attribution") or {},
        "stalls": bridge_doc.get("stall_correlation") or [],
        "lag": (summary.get("metadata") or {}).get("lag_diagnosis") or {},
        "gcinfo": (summary.get("metadata") or {}).get("gc") or {},
        "transfers": (summary.get("metadata") or {}).get("transfers") or {},
        "net": (summary.get("metadata") or {}).get("net") or {},
        "churn_sites": (summary.get("metadata") or {}).get("top_churn_sites") or [],
        "alloc_sites": (summary.get("metadata") or {}).get("top_alloc_sites") or [],
        "worldinfo": (summary.get("metadata") or {}).get("world") or {},
        "frameinfo": (summary.get("metadata") or {}).get("frame") or {},
        "links": [(label, href) for label, href in candidates if (session / href).is_file()],
    }
    atomic_text(session / "report.html", ENV.get_template("report.html.j2").render(**context))
    atomic_text(session / "dashboard.html", ENV.get_template("dashboard.html.j2").render(**context))
