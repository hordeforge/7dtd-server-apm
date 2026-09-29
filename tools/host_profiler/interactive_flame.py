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

# apm_suite is resolved from the repository checkout, not the interpreter's
# venv: these scripts run under the resolved project interpreter (shell
# callers go through scripts/lib/python.sh -> SEVENDTD_APM_PYTHON), which is not
# necessarily this interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apm_suite.io import force_utf8_stdio
from apm_suite.web_tokens import FAVICON_HREF, TILE_SVG, base_css

# reuse converters
sys.path.insert(0, str(Path(__file__).resolve().parent))
from folded_to_speedscope import dumps_deep, load_folded, to_d3_tree

# The flame page is a fifth view of the same product as the report, dashboard,
# and session index, so it reads the same tokens instead of a hand-copied
# palette that drifts the moment a token changes.
FLAME_CSS = """
  * { box-sizing: border-box; }
  /* The chart runs edge to edge under its own header, so the base page gutter
     is dropped rather than left as a second 24px inset. Palette, type scale,
     and the flat unboxed look all come from the shared base sheet above; a
     flame page that redraws them is how the views drift into separate
     products. */
  body { margin:0; padding:0; }
  header { padding:12px 16px; background:var(--apm-surface); display:flex; flex-wrap:wrap; gap:8px 12px; align-items:center; border-bottom:1px solid var(--apm-rule); }
  header .title { display:flex; align-items:center; gap:10px; min-width:0; }
  header .tile { flex:none; }
  header h1 { font-size:var(--apm-h1); margin:0; }
  header .muted { color:var(--apm-muted); font-size:var(--apm-code); }
  #controls { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-left:auto; }
  /* Control borders use the secondary text tone: the hairline rule is below
     the 3:1 non-text contrast a control boundary needs (WCAG 1.4.11). */
  input[type=search] { background:var(--apm-bg); border:1px solid var(--apm-muted); color:var(--apm-text); font:inherit; padding:6px 10px; width:260px; max-width:100%; min-height:32px; }
  input[type=search]::placeholder { color:var(--apm-muted); }
  button { background:var(--apm-bg); border:1px solid var(--apm-muted); color:var(--apm-text); font:inherit; padding:6px 12px; min-height:32px; cursor:pointer; }
  button:hover { border-color:var(--apm-link); }
  #breadcrumb { padding:8px 16px; font-size:var(--apm-code); color:var(--apm-muted); word-break:break-all; min-height:1.5em; }
  #breadcrumb a { color:var(--apm-link); cursor:pointer; text-decoration:underline; margin-right:4px; }
  #breadcrumb a:hover, #breadcrumb a:focus-visible { text-decoration:none; }
  #chart { width:100%; overflow:hidden; }
  #chart svg { display:block; width:100%; }
  .frame rect { stroke:var(--apm-bg); stroke-width:0.5; cursor:pointer; }
  /* Frame labels are dark ink on the warm frame fills, and 11px is chart ink,
     not page type: the scale does not govern a label inside the plot. */
  .frame text { font-family: var(--apm-mono); font-size:11px; fill:var(--apm-bg); pointer-events:none; }
  .frame.dim rect { opacity:0.25; }
  .frame.hit rect { stroke:var(--apm-accent); stroke-width:1.5; }
  .frame:focus { outline:none; }
  .frame:focus-visible rect { stroke:var(--apm-text); stroke-width:2; }
  #tip {
    display:none; position:fixed; z-index:10; background:var(--apm-surface); border:1px solid var(--apm-rule);
    padding:8px 10px; font-size:var(--apm-code); max-width:480px; pointer-events:none;
  }
  #tip b { color:var(--apm-accent); word-break:break-all; }
  footer { padding:8px 16px; font-size:var(--apm-code); color:var(--apm-muted); }
  footer a { color:var(--apm-link); }
"""

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="__FAVICON__">
<style>__CSS__</style>
</head>
<body>
<header>
  <div class="title">__TILE__<h1>__TITLE__</h1></div>
  <span class="muted" id="meta"></span>
  <div id="controls">
    <input type="search" id="q" placeholder="Search frames…" aria-label="Search frames" autocomplete="off">
    <span id="search-status" class="muted"></span>
    <button type="button" id="reset">Reset zoom</button>
    <button type="button" id="pct">Show sample counts</button>
  </div>
</header>
<div id="breadcrumb"></div>
<div id="chart"></div>
<div id="tip"></div>
<span id="sr-status" role="status" class="sr-only"></span>
<footer>
  Click a frame to zoom, or a breadcrumb above the chart to step back (Tab to a frame and press Enter works too). Esc resets. Search highlights matches.
  Also open <code>__SPEEDSCOPE_NAME__</code> in
  <a href="https://www.speedscope.app/" target="_blank" rel="noopener">speedscope.app</a>
  or <code>bunx speedscope __SPEEDSCOPE_NAME__</code>
</footer>
<script>
const ROOT = __TREE_JSON__;
const H = 18;
const PAD = 2;
let showTotal = true;
let focus = ROOT;
let search = "";
// Every zoom the reader has made, root first. The breadcrumb renders it, so a
// deep stack shows where the reader is and each ancestor is one click back;
// without the trail a zoomed chart names only the leaf and Reset is the sole
// way out.
let trail = [ROOT];
// Frames rendered by the most recent render(), indexed by data-i. Event
// handlers are delegated on #chart (bound once, survive innerHTML swaps)
// and resolve back to their frame through this array.
let nodes = [];

const chart = document.getElementById("chart");
const tip = document.getElementById("tip");
const crumb = document.getElementById("breadcrumb");
const meta = document.getElementById("meta");
const searchStatus = document.getElementById("search-status");
meta.textContent = `samples=${ROOT.value}`;

// Search feedback is visible, not screen-reader-only: dimming every non-matching
// frame looks like a rendering bug when the term simply has no hits.
function countMatches() {
  let hits = 0;
  (function walk(n) { if (matches(n)) hits++; (n.children || []).forEach(walk); })(ROOT);
  return hits;
}

function showSearchStatus(hits) {
  if (!search) { searchStatus.textContent = ""; return; }
  searchStatus.textContent = hits === 0 ? "no frames match" : `${hits} frame${hits === 1 ? "" : "s"}`;
}

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

  let   svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="group" aria-label="Flamegraph chart: each frame is a focusable button">`;
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
  const g = frameElementAt(target);
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
  tip.innerHTML = `<b>${escapeXml(n.node.name)}</b><br/>samples: ${n.node.value}<br/>of zoom: ${n.pct}%<br/>of total: ${ofRoot}%`;
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
  crumb.innerHTML = trail.map((n, i) => {
    const name = i === 0 ? "all" : escapeXml(n.name);
    return i === trail.length - 1 ? `<span>${escapeXml(n.name)}</span>` : `<a href="#" data-i="${i}">${name}</a>`;
  }).join(" / ") + ` (${focus.value} samples)`;
}

// Each breadcrumb crumb re-zooms to that ancestor; zoomTo truncates the trail
// there, so the path never grows stale behind the reader.
crumb.addEventListener("click", ev => {
  const link = ev.target instanceof Element ? ev.target.closest("a[data-i]") : null;
  if (!link) return;
  ev.preventDefault();
  const node = trail[+link.getAttribute("data-i")];
  if (node) zoomTo(node, node === ROOT ? "the whole profile" : node.name, false);
});

// Re-render around a node and tell assistive tech what happened.
function zoomTo(node, what, viaKeyboard) {
  // Truncate at the target when it is already an ancestor crumb, otherwise
  // cut back to the current focus and push: the trail stays a real path from
  // the root in both directions.
  const seen = trail.indexOf(node);
  if (seen >= 0) {
    trail = trail.slice(0, seen + 1);
  } else {
    const at = trail.indexOf(focus);
    trail = trail.slice(0, at < 0 ? 1 : at + 1);
    trail.push(node);
  }
  focus = node;
  render();
  updateCrumb();
  announce(`Zoomed to ${what}`);
  // render() replaced the DOM, dropping keyboard focus; put it back on the new
  // zoom root so keyboard users are not thrown back to the page top.
  if (viaKeyboard) {
    const first = chart.querySelector(".frame");
    if (first) first.focus();
  }
}

// Back to the root: the trail, the search box, and the zoom all reset, so the
// next click starts from the same state the page opened in.
function resetZoom(what) {
  focus = ROOT;
  trail = [ROOT];
  clearSearch();
  render();
  announce(what);
}

// Announce a message to screen readers via the role=status live region.
// (Named announce, not status: window.status already exists.)
function announce(msg) {
  document.getElementById("sr-status").textContent = msg;
}

function escapeXml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function clearSearch() {
  search = "";
  document.getElementById("q").value = "";
  showSearchStatus(0);
}

document.getElementById("reset").onclick = () => resetZoom("Reset zoom, showing the whole profile");
document.getElementById("pct").onclick = () => {
  showTotal = !showTotal;
  render();
  announce(showTotal ? "Showing percent of total" : "Showing sample counts");
  // The label names what the click will do, the same shape the panel's
  // "Timescale: compressed" toggle uses.
  document.getElementById("pct").textContent = showTotal ? "Show sample counts" : "Show % total";
};
document.getElementById("q").addEventListener("input", e => {
  search = (e.target.value || "").trim().toLowerCase();
  // One walk answers both the visible count and the announcement; the render
  // itself stays coalesced so typing does not rebuild the SVG twice a keystroke.
  const hits = search ? countMatches() : 0;
  showSearchStatus(hits);
  if (!search) { scheduleRender(); return; }
  scheduleRender(() => announce(hits === 0
    ? `No frames match "${search}"`
    : `${hits} frame${hits === 1 ? "" : "s"} match "${search}"`));
});
window.addEventListener("keydown", e => {
  if (e.key === "Escape") { resetZoom("Reset zoom"); }
});
window.addEventListener("resize", () => scheduleRender());
render();
</script>
</body>
</html>
"""


def build_html(tree_json: str, title: str, speedscope_name: str) -> str:
    """Fill the page template. `tree_json` must already be < and > escaped."""
    return (
        HTML.replace("__TITLE__", html.escape(title))
        .replace("__CSS__", base_css(FLAME_CSS))
        .replace("__FAVICON__", FAVICON_HREF)
        .replace("__TILE__", TILE_SVG)
        .replace("__TREE_JSON__", tree_json)
        .replace("__SPEEDSCOPE_NAME__", html.escape(speedscope_name))
    )


def main() -> int:
    force_utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, nargs="?", help="stacks.folded")
    ap.add_argument("--tree", type=Path, help="d3 tree JSON instead of folded")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--title", default="Geiger flamegraph")
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
    page = build_html(tree_json, args.title, args.speedscope_name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(f"wrote {args.output} (open in browser; click to zoom)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
