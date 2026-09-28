#!/usr/bin/env python3
"""Build a simple HTML table+bars flame frame diff between two sessions."""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

# apm_suite is resolved from the repository checkout, not the interpreter's
# venv: these scripts run under the resolved project interpreter (shell
# callers go through scripts/lib/python.sh -> SEVENDTD_APM_PYTHON), which is not
# necessarily this interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apm_suite.analysis.flame_delta import delta, folded_stack_path, load_weights
from apm_suite.io import force_utf8_stdio
from apm_suite.web_tokens import base_css

FLAME_CSS = """
main{max-width:1400px;margin:0 auto}
main>p{margin:.4rem 0}
.delta-bad{color:var(--apm-bad)}
.delta-ok{color:var(--apm-ok)}
.mag{height:6px;min-width:1px}
.mag.bad{background:var(--apm-bad)}
.mag.ok{background:var(--apm-ok)}
"""

# Column cap, in characters (Python str units on a decoded frame name, not bytes
# and not terminal columns): a frame name is a single unit in a <code> cell that
# wraps, so the cut is a readability bound and the ellipsis-free tail is never
# decoded again as a partial UTF-8 sequence.
FRAME_CELL_CHARS = 90


def build_html(a: Path, b: Path, rows: list[dict[str, Any]]) -> str:
    tr = []
    max_abs = max((abs(r["delta"]) for r in rows), default=1) or 1
    for r in rows:
        w = 100 * abs(r["delta"]) / max_abs
        # A frame that got heavier is the regression this page exists to find,
        # so it carries the same bad/good token the rest of the product uses.
        tone = "bad" if r["delta"] > 0 else "ok"
        tr.append(
            f"<tr><td><code>{html.escape(r['frame'][:FRAME_CELL_CHARS], quote=True)}</code></td>"
            f'<td class="num">{r["a"]}</td><td class="num">{r["b"]}</td>'
            f'<td class="num delta-{tone}">{r["delta"]:+}</td>'
            f'<td><div aria-hidden="true" class="mag {tone}" style="width:{w:.1f}%"></div></td></tr>'
        )
    if not rows:
        # An empty table of headers is a dead end: the reader cannot tell a
        # no-difference result from a page that failed to build. Say which.
        tr.append(
            '<tr><td class="empty" colspan="5">No frames differ between these '
            "two sessions (no common frames with a non-zero delta).</td></tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>Flame delta</title>
<style>{base_css(FLAME_CSS)}</style></head><body>
<main>
<h1>Speedscope / folded frame delta</h1>
<p class="muted">A={html.escape(str(a), quote=True)}<br/>B={html.escape(str(b), quote=True)}<br/>
Negative Δ = frame weight dropped in B (usually good for hot GC/locks).</p>
<p><a href="dashboard.html">Dashboard</a> · <a href="report.html">Report</a> · <a href="../index.html">All sessions</a></p>
<div class="scroll"><table>
<caption class="sr-only">Frame weight delta between sessions A and B</caption>
<tr><th scope="col">Frame</th><th scope="col" class="num">A</th><th scope="col" class="num">B</th><th scope="col" class="num">Δ</th><th scope="col">Relative Δ magnitude</th></tr>
{"".join(tr)}
</table></div>
</main>
</body></html>
"""


def main() -> int:
    force_utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session_a", type=Path)
    ap.add_argument("session_b", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()
    fa, fb = folded_stack_path(args.session_a), folded_stack_path(args.session_b)
    if not fa or not fb:
        print("both sessions need cpu/perf/stacks.folded", file=sys.stderr)
        return 2
    rows = delta(load_weights(fa), load_weights(fb), top=args.top)
    html = build_html(args.session_a, args.session_b, rows)
    out = args.output or (args.session_b / "flame_diff.html")
    out.write_text(html, encoding="utf-8")
    (args.session_b / "flame_diff.json").write_text(
        json.dumps({"frames": rows}, indent=2), encoding="utf-8"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
