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
from folded_to_speedscope import dumps_deep, load_folded, to_d3_tree

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>__TITLE__</title>
<style>
  /* Shared APM web tokens (report + dashboard + session index + flame pages). */
  :root { --bg:#0f1115; --panel:#161a22; --fg:#e8eaed; --muted:#9aa0a6; --accent:#8ab4f8; --hi:#fdd663; --rule:#303642; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:12px 16px; background:var(--panel); display:flex; flex-wrap:wrap; gap:12px; align-items:center; border-bottom:1px solid var(--rule); }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  header .muted { color:var(--muted); font-size:13px; }
  #controls { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-left:auto; }
  input[type=search] { background:var(--bg); border:1px solid var(--rule); color:var(--fg); padding:6px 10px; border-radius:6px; min-width:200px; }
  button { background:#2a2f3a; border:1px solid var(--rule); color:var(--fg); padding:6px 12px; border-radius:6px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  #breadcrumb { padding:8px 16px; font-size:12px; color:var(--muted); word-break:break-all; min-height:1.5em; }
  #breadcrumb a { color:var(--accent); cursor:pointer; text-decoration:none; margin-right:4px; }
  #chart { width:100%; overflow:hidden; }
  svg { display:block; width:100%; }
  .frame rect { stroke:var(--bg); stroke-width:0.5; cursor:pointer; }
  .frame text { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:11px; fill:#111; pointer-events:none; }
  .frame.dim rect { opacity:0.25; }
  .frame.hit rect { stroke:var(--hi); stroke-width:1.5; }
  #tip {
    display:none; position:fixed; z-index:10; background:var(--panel); border:1px solid var(--rule);
    padding:8px 10px; border-radius:6px; font-size:12px; max-width:480px; pointer-events:none;
    box-shadow:0 4px 16px rgba(0,0,0,.4);
  }
  #tip b { color:var(--hi); }
  .sr-only { position:absolute; width:1px; height:1px; margin:-1px; padding:0; border:0; clip-path:inset(50%); overflow:hidden; white-space:nowrap; }
  footer { padding:8px 16px; font-size:12px; color:var(--muted); }
  footer a { color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="muted" id="meta"></span>
  <div id="controls">
    <input type="search" id="q" placeholder="Search frames…" aria-label="Search frames" autocomplete="off"/>
    <button type="button" id="reset">Reset zoom</button>
    <button type="button" id="pct">Toggle % total / self</button>
  </div>
</header>
<div id="breadcrumb"></div>
<div id="chart"></div>
<div id="tip"></div>
<span id="sr-status" role="status" class="sr-only"></span>
<footer>
  Click a frame to zoom (or Tab to it and press Enter). Esc resets. Search highlights matches.
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
// Frames rendered by the most recent render(), indexed by data-i. Event
// handlers are delegated on #chart (bound once, survive innerHTML swaps)
// and resolve back to their frame through this array.
let nodes = [];

const chart = document.getElementById("chart");
const tip = document.getElementById("tip");
const crumb = document.getElementById("breadcrumb");
const meta = document.getElementById("meta");
meta.textContent = `samples=${ROOT.value}`;

// Coalesce render requests to one per animation frame: a resize drag or
// search-as-you-type otherwise rebuilds the whole SVG many times per second.
let renderQueued = false;
function scheduleRender(after) {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    render();
    if (after) after();
  });
}

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

const NO_MARK = Object.freeze({ hit: false, dim: false });

// One pass over the tree computing per-node highlight state: hit = the node
// itself matches, dim = nothing in its subtree matches. Doing this up front
// keeps place() O(1) per node instead of rescanning the subtree of every
// node while a search filter is active.
function markMatches(node, marks) {
  let subtreeHit = matches(node);
  const kids = node.children || [];
  for (const c of kids) {
    if (markMatches(c, marks)) subtreeHit = true;
  }
  const mark = { hit: matches(node), dim: !subtreeHit };
  marks.set(node, mark);
  return subtreeHit;
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
  nodes = [];
  const marks = new Map();
  if (search) markMatches(focus, marks);

  function place(node, x0, x1, depth) {
    const w = x1 - x0;
    if (w < 0.5) return;
    const y = depth * H;
    const mark = search ? marks.get(node) : NO_MARK;
    const pct = (100 * node.value / total).toFixed(2);
    const label = showTotal
      ? `${node.name} (${pct}%)`
      : `${node.name} (n=${node.value})`;
    nodes.push({ node, x0, x1, y, w, hit: mark.hit, dim: mark.dim, label, pct });
    const kids = node.children || [];
    let x = x0;
    for (const c of kids) {
      const cw = w * (c.value / node.value);
      place(c, x, x + cw, depth + 1);
      x += cw;
    }
  }
  place(focus, 0, width, 0);

  for (let i = 0; i < nodes.length; i++) {
    const n = nodes[i];
    const cls = "frame" + (n.dim ? " dim" : "") + (n.hit ? " hit" : "");
    const showText = n.w > 40;
    const text = showText ? escapeXml(n.label.slice(0, Math.floor(n.w / 7))) : "";
    // Keyboard access: every visible frame is a focusable button-like node
    // with a full accessible name (WCAG 2.1.1 / 4.1.2); Enter/Space zooms.
    const kbd = ` tabindex="0" role="button" aria-label="${escapeXml(n.label)}"`;
    svg += `<g class="${cls}" data-i="${i}"${kbd}>`;
    svg += `<rect x="${n.x0.toFixed(2)}" y="${n.y}" width="${Math.max(n.w - 0.5, 0.5).toFixed(2)}" height="${H - PAD}" fill="${color(n.node.name)}"/>`;
    if (text)
      svg += `<text x="${(n.x0 + 3).toFixed(2)}" y="${n.y + 12}">${text}</text>`;
    svg += `</g>`;
  }
  svg += `</svg>`;
  chart.innerHTML = svg;

  updateCrumb();
}

function frameAt(target) {
  const g = target instanceof Element ? target.closest(".frame") : null;
  return g ? nodes[+g.getAttribute("data-i")] : null;
}

function frameElementAt(target) {
  return target instanceof Element ? target.closest(".frame") : null;
}

function showTipFor(n, x, y) {
  tip.style.display = "block";
  tip.style.left = Math.min(x + 12, window.innerWidth - 300) + "px";
  tip.style.top = (y + 12) + "px";
  const ofRoot = (100 * n.node.value / ROOT.value).toFixed(2);
  tip.innerHTML = `<b>${escapeHtml(n.node.name)}</b><br/>samples: ${n.node.value}<br/>of zoom: ${n.pct}%<br/>of total: ${ofRoot}%`;
}

// Delegated frame interactions (bound once): pointer and keyboard events
// resolve through .frame[data-i] instead of rebinding five listeners per
// frame on every render.
chart.addEventListener("click", ev => {
  const n = frameAt(ev.target);
  if (n) zoomTo(n.node, `${n.node.name} (${n.node.value} samples)`, ev.detail === 0);
});
chart.addEventListener("keydown", ev => {
  if (ev.key !== "Enter" && ev.key !== " ") return;
  const n = frameAt(ev.target);
  if (n) { ev.preventDefault(); zoomTo(n.node, `${n.node.name} (${n.node.value} samples)`, true); }
});
chart.addEventListener("mousemove", ev => {
  const n = frameAt(ev.target);
  if (!n) { tip.style.display = "none"; return; }
  showTipFor(n, ev.clientX, ev.clientY);
});
chart.addEventListener("mouseleave", () => { tip.style.display = "none"; });
chart.addEventListener("focusin", ev => {
  const g = frameElementAt(ev.target);
  if (!g) return;
  const n = nodes[+g.getAttribute("data-i")];
  if (!n) return;
  const r = g.querySelector("rect").getBoundingClientRect();
  showTipFor(n, Math.min(r.left + r.width / 2, window.innerWidth - 300), r.bottom);
});
chart.addEventListener("focusout", () => { tip.style.display = "none"; });

function updateCrumb() {
  // simple path: only show current focus name chain is hard without parent links; show focus name
  crumb.innerHTML = `<a href="#" data-root="1">all</a> / <span>${escapeHtml(focus.name)}</span> (${focus.value} samples)`;
  crumb.querySelector("[data-root]").onclick = (ev) => { ev.preventDefault(); zoomTo(ROOT, "the whole profile"); };
}

// Re-render around a node and tell assistive tech what happened. `what` is an
// optional screen-reader announcement; the visual state is the same either way.
function zoomTo(node, what, viaKeyboard) {
  focus = node;
  render();
  updateCrumb();
  if (what === undefined) return;
  announce(`Zoomed to ${what}`);
  // render() replaced the DOM, dropping keyboard focus; put it back on the new
  // zoom root so keyboard users are not thrown back to the page top.
  if (viaKeyboard) {
    const first = chart.querySelector(".frame");
    if (first) first.focus();
  }
}

// Announce a message to screen readers via the role=status live region.
// (Named announce, not status: window.status already exists.)
function announce(msg) {
  document.getElementById("sr-status").textContent = msg;
}

function escapeXml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function escapeHtml(s) {
  return escapeXml(s);
}

document.getElementById("reset").onclick = () => { focus = ROOT; search = ""; document.getElementById("q").value = ""; render(); announce("Reset zoom, showing the whole profile"); };
document.getElementById("pct").onclick = () => { showTotal = !showTotal; render(); announce(showTotal ? "Showing percent of total" : "Showing self samples"); };
document.getElementById("q").addEventListener("input", e => {
  search = (e.target.value || "").trim().toLowerCase();
  // Coalesced with the render so typing does not walk the tree twice per
  // keystroke (once to redraw, once to count matches).
  scheduleRender(() => {
    if (!search) return;
    let hits = 0;
    (function count(n) { if (matches(n)) hits++; (n.children || []).forEach(count); })(ROOT);
    announce(`${hits} frame${hits === 1 ? "" : "s"} match "${search}"`);
  });
});
window.addEventListener("keydown", e => {
  if (e.key === "Escape") { focus = ROOT; search = ""; document.getElementById("q").value = ""; render(); announce("Reset zoom"); }
});
window.addEventListener("resize", () => scheduleRender());
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
        tree = json.loads(args.tree.read_text(encoding="utf-8"))
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
        dumps_deep(tree).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    page = (
        HTML.replace("__TITLE__", html.escape(args.title))
        .replace("__TREE_JSON__", tree_json)
        .replace("__SPEEDSCOPE_NAME__", html.escape(args.speedscope_name))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(f"wrote {args.output} (open in browser; click to zoom)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
