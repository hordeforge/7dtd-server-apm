#!/usr/bin/env python3
"""Build a simple HTML table+bars flame frame diff between two sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Every script in this directory runs under a bare python3 (make_flames.sh,
# perf_record.sh); keep that contract here by resolving apm_suite from the
# repository checkout when it is not installed in the interpreter's venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apm_suite.analysis.flame_delta import delta, load_weights


def flame_path(session: Path) -> Path | None:
    for rel in (
        "cpu/perf/stacks.annotated.folded",
        "cpu/perf/stacks.folded",
    ):
        p = session / rel
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def build_html(a: Path, b: Path, rows: list[dict]) -> str:
    tr = []
    max_abs = max((abs(r["delta"]) for r in rows), default=1) or 1
    for r in rows:
        w = 100 * abs(r["delta"]) / max_abs
        color = "#ff7070" if r["delta"] > 0 else "#57d977"
        tr.append(
            f"<tr><td><code>{_esc(r['frame'][:90])}</code></td>"
            f"<td>{r['a']}</td><td>{r['b']}</td>"
            f"<td style='color:{color}'>{r['delta']:+}</td>"
            f"<td><div aria-hidden='true' style='background:{color};height:12px;width:{w:.1f}%'></div></td></tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>Flame delta</title>
<style>
/* Shared APM web tokens (report + dashboard + session index + flame pages):
   bg #0f1115 · surface #161a22 · outline #2a2f3a · rule #303642 ·
   text #e8eaed · muted #9aa0a6 · link #8ab4f8 · accent #e6bd3a */
body{{font-family:system-ui;background:#0f1115;color:#e8eaed;margin:24px}}
a{{color:#8ab4f8}}
code{{font-size:12px}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #303642;padding:6px;text-align:left}} th{{background:#161a22}}
.muted{{color:#9aa0a6}}
</style></head><body>
<h1>Speedscope / folded frame delta</h1>
<p class="muted">A={_esc(str(a))}<br/>B={_esc(str(b))}<br/>
Negative Δ = frame weight dropped in B (usually good for hot GC/locks).</p>
<p><a href="dashboard.html">Dashboard</a> · <a href="../index.html">All sessions</a></p>
<table>
<tr><th scope="col">Frame</th><th scope="col">A</th><th scope="col">B</th><th scope="col">Δ</th><th scope="col">Relative Δ magnitude</th></tr>
{"".join(tr)}
</table>
</body></html>
"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session_a", type=Path)
    ap.add_argument("session_b", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()
    fa, fb = flame_path(args.session_a), flame_path(args.session_b)
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
