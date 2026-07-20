#!/usr/bin/env python3
"""Build self-contained interactive flamegraph HTML from folded stacks or tree JSON.

Features: click-to-zoom, search highlight, tooltip, reset, % of parent / total.
No CDN required (works offline).

Usage:
  python3 interactive_flame.py stacks.folded -o flame.html
  python3 interactive_flame.py --tree flame.tree.json -o flame.html
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

# reuse converters
sys.path.insert(0, str(Path(__file__).resolve().parent))
from folded_to_speedscope import load_folded, to_d3_tree  # noqa: E402

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>__TITLE__</title>
<style>
  :root { --bg:#0f1115; --panel:#1a1d24; --fg:#e8eaed; --muted:#9aa0a6; --accent:#8ab4f8; --hi:#fdd663; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:12px 16px; background:var(--panel); display:flex; flex-wrap:wrap; gap:12px; align-items:center; border-bottom:1px solid #333; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  header .muted { color:var(--muted); font-size:13px; }
  #controls { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-left:auto; }
  input[type=search] { background:#0f1115; border:1px solid #444; color:var(--fg); padding:6px 10px; border-radius:6px; min-width:200px; }
  button { background:#2a2f3a; border:1px solid #444; color:var(--fg); padding:6px 12px; border-radius:6px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  #breadcrumb { padding:8px 16px; font-size:12px; color:var(--muted); word-break:break-all; min-height:1.5em; }
  #breadcrumb a { color:var(--accent); cursor:pointer; text-decoration:none; margin-right:4px; }
  #chart { width:100%; overflow:hidden; }
  svg { display:block; width:100%; }
  .frame rect { stroke:#0f1115; stroke-width:0.5; cursor:pointer; }
  .frame text { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:11px; fill:#111; pointer-events:none; }
  .frame.dim rect { opacity:0.25; }
  .frame.hit rect { stroke:var(--hi); stroke-width:1.5; }
  #tip {
    display:none; position:fixed; z-index:10; background:#202124; border:1px solid #555;
    padding:8px 10px; border-radius:6px; font-size:12px; max-width:480px; pointer-events:none;
    box-shadow:0 4px 16px rgba(0,0,0,.4);
  }
  #tip b { color:var(--hi); }
  footer { padding:8px 16px; font-size:12px; color:var(--muted); }
  footer a { color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="muted" id="meta"></span>
  <div id="controls">
    <input type="search" id="q" placeholder="Search frames…" autocomplete="off"/>
    <button type="button" id="reset">Reset zoom</button>
    <button type="button" id="pct">Toggle % total / self</button>
  </div>
</header>
<div id="breadcrumb"></div>
<div id="chart"></div>
<div id="tip"></div>
<footer>
  Click a frame to zoom. Esc resets. Search highlights matches.
  Also open <code>__SPEEDSCOPE_NAME__</code> in
  <a href="https://www.speedscope.app/" target="_blank" rel="noopener">speedscope.app</a>
  or <code>npx speedscope __SPEEDSCOPE_NAME__</code>
</footer>
<script>
const ROOT = __TREE_JSON__;
const H = 18;
const PAD = 2;
let showTotal = true;
let focus = ROOT;
let search = "";

const chart = document.getElementById("chart");
const tip = document.getElementById("tip");
const crumb = document.getElementById("breadcrumb");
const meta = document.getElementById("meta");
meta.textContent = `samples=${ROOT.value}`;

function color(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 33 + name.charCodeAt(i)) >>> 0;
  const r = 180 + (h & 55);
  const g = 80 + ((h >> 8) & 100);
  const b = 40 + ((h >> 16) & 60);
  return `rgb(${r},${g},${b})`;
}

function matches(node) {
  if (!search) return false;
  return node.name.toLowerCase().includes(search);
}

function anyMatch(node) {
  if (matches(node)) return true;
  return (node.children || []).some(anyMatch);
}

function render() {
  const width = Math.max(chart.clientWidth || window.innerWidth, 640);
  // depth
  let maxD = 0;
  (function walk(n, d) {
    maxD = Math.max(maxD, d);
    (n.children || []).forEach(c => walk(c, d + 1));
  })(focus, 0);
  const height = (maxD + 2) * H + 20;
  const total = focus.value || 1;

  let svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`;
  const nodes = [];

  function place(node, x0, x1, depth) {
    const w = x1 - x0;
    if (w < 0.5) return;
    const y = depth * H;
    const hit = search ? matches(node) : false;
    const dim = search ? !anyMatch(node) : false;
    const pct = (100 * node.value / total).toFixed(2);
    const label = showTotal
      ? `${node.name} (${pct}%)`
      : `${node.name} (n=${node.value})`;
    nodes.push({ node, x0, x1, y, w, hit, dim, label, pct });
    const kids = node.children || [];
    let x = x0;
    for (const c of kids) {
      const cw = w * (c.value / node.value);
      place(c, x, x + cw, depth + 1);
      x += cw;
    }
  }
  place(focus, 0, width, 0);

  for (const n of nodes) {
    const cls = "frame" + (n.dim ? " dim" : "") + (n.hit ? " hit" : "");
    const showText = n.w > 40;
    const text = showText ? escapeXml(n.label.slice(0, Math.floor(n.w / 7))) : "";
    svg += `<g class="${cls}" data-i="${nodes.indexOf(n)}">`;
    svg += `<rect x="${n.x0.toFixed(2)}" y="${n.y}" width="${Math.max(n.w - 0.5, 0.5).toFixed(2)}" height="${H - PAD}" fill="${color(n.node.name)}"/>`;
    if (text)
      svg += `<text x="${(n.x0 + 3).toFixed(2)}" y="${n.y + 12}">${text}</text>`;
    svg += `</g>`;
  }
  svg += `</svg>`;
  chart.innerHTML = svg;

  chart.querySelectorAll(".frame").forEach(g => {
    const i = +g.getAttribute("data-i");
    const n = nodes[i];
    g.addEventListener("click", () => {
      focus = n.node;
      render();
      updateCrumb();
    });
    g.addEventListener("mousemove", ev => {
      tip.style.display = "block";
      tip.style.left = Math.min(ev.clientX + 12, window.innerWidth - 300) + "px";
      tip.style.top = (ev.clientY + 12) + "px";
      const ofRoot = (100 * n.node.value / ROOT.value).toFixed(2);
      tip.innerHTML = `<b>${escapeHtml(n.node.name)}</b><br/>samples: ${n.node.value}<br/>of zoom: ${n.pct}%<br/>of total: ${ofRoot}%`;
    });
    g.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  });
  updateCrumb();
}

function updateCrumb() {
  // simple path: only show current focus name chain is hard without parent links; show focus name
  crumb.innerHTML = `<a data-root="1">all</a> / <span>${escapeHtml(focus.name)}</span> (${focus.value} samples)`;
  crumb.querySelector("[data-root]").onclick = () => { focus = ROOT; render(); };
}

function escapeXml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function escapeHtml(s) {
  return escapeXml(s);
}

document.getElementById("reset").onclick = () => { focus = ROOT; search = ""; document.getElementById("q").value = ""; render(); };
document.getElementById("pct").onclick = () => { showTotal = !showTotal; render(); };
document.getElementById("q").addEventListener("input", e => {
  search = (e.target.value || "").trim().toLowerCase();
  render();
});
window.addEventListener("keydown", e => {
  if (e.key === "Escape") { focus = ROOT; search = ""; document.getElementById("q").value = ""; render(); }
});
window.addEventListener("resize", () => render());
render();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, nargs="?", help="stacks.folded")
    ap.add_argument("--tree", type=Path, help="d3 tree JSON instead of folded")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--title", default="7dtd interactive flamegraph")
    ap.add_argument("--speedscope-name", default="profile.speedscope.json")
    args = ap.parse_args()

    if args.tree:
        tree = json.loads(args.tree.read_text())
    elif args.input:
        rows = load_folded(args.input)
        if not rows:
            print("no stacks", file=sys.stderr)
            return 1
        tree = to_d3_tree(rows)
    else:
        print("need folded file or --tree", file=sys.stderr)
        return 2

    # Embed the tree JSON inside <script>. Frame names come from JIT/perf and are
    # not fully trusted: a name containing "</script>" would break out of the tag
    # and execute (stored XSS when the report is opened). Escape the HTML-significant
    # characters as \uXXXX (still valid JSON/JS, no <script> break-out).
    tree_json = (
        json.dumps(tree).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    page = (
        HTML.replace("__TITLE__", html.escape(args.title))
        .replace("__TREE_JSON__", tree_json)
        .replace("__SPEEDSCOPE_NAME__", html.escape(args.speedscope_name))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page)
    print(f"wrote {args.output} (open in browser; click to zoom)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
