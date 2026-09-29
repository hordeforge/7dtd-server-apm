from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .io import atomic_text, load_json
from .web_tokens import BRAND_MARK, DASHBOARD_CSS, FAVICON_HREF, REPORT_CSS, base_css

TEMPLATES = Path(__file__).with_name("templates")
ENV = Environment(
    loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(("html", "xml", "j2"))
)


def _load(path: Path) -> dict[str, Any]:
    """Best-effort JSON load. A malformed artifact must not crash rendering (the
    report is a summary, not a source of truth) - degrade to an empty dict, and
    say so: an empty page that reads as a healthy session is the one outcome a
    render fallback must not produce silently. load_json already names the file
    and the parse failure in its message."""
    if not path.is_file():
        return {}
    try:
        return load_json(path)
    except (ValueError, OSError) as error:
        print(
            f"finalize: report input unusable, rendering without {path.name}: {error}",
            file=sys.stderr,
        )
        return {}


def render_session(session: Path) -> None:
    summary = _load(session / "summary.json")
    health = _load(session / "health.json") or summary.get("health") or {}
    events_doc = _load(session / "events.json")
    bridge_doc = _load(session / "csharp_bridge.json")
    # meta.json is the source of truth for session metadata; summary.json only
    # carries an embedded copy. The copy is the fallback, never `metadata`:
    # that key is the ANALYSIS block (lag_diagnosis, net, frame, ...), a
    # different shape that would silently feed the templates a dict of
    # diagnoses where pid/utc are expected.
    meta = _load(session / "meta.json") or summary.get("meta") or {}
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
        "brand_mark": BRAND_MARK,
        "favicon": FAVICON_HREF,
    }
    atomic_text(
        session / "report.html",
        ENV.get_template("report.html.j2").render(**context, css=base_css(REPORT_CSS)),
    )
    atomic_text(
        session / "dashboard.html",
        ENV.get_template("dashboard.html.j2").render(**context, css=base_css(DASHBOARD_CSS)),
    )
