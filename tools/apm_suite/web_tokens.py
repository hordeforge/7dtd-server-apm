"""The APM web design tokens and the base stylesheet every generated page uses.

Report, dashboard, session index, flame delta, and the interactive flamegraph
are five views of one product (Geiger). One source, rendered into every page,
keeps them from drifting into five different looks.

The values are the HordeForge brand terminal palette (`.github/brand/tokens.css`,
the `--term-*` block): a dark data surface, one signal green, one red, one key
amber. Surfaces stay flat and hairline rules separate them. State is always a
word on the page; a color only reinforces it.
"""

from __future__ import annotations

from urllib.parse import quote

TOKENS: dict[str, str] = {
    # Surfaces: page, one step up for header bands and table heads, hairlines.
    "bg": "#101418",
    "surface": "#1a2129",
    "rule": "#2a333d",
    # Text: primary and secondary.
    "text": "#d8e2dc",
    "muted": "#7f8b94",
    # Signal green is the one action color (links, focus) and the ok state.
    # There is no separate near-limit tone, so a bar and the number beside it
    # cannot disagree.
    "link": "#5fd894",
    "ok": "#5fd894",
    "bad": "#ff7364",
    # Key amber marks a budgeted number and the warning state.
    "accent": "#ffd8a0",
    # The Geiger product tile: observability suite ground, paper glyph. Used
    # by the mark only, never for state.
    "tile": "#1f4e8c",
    "paper": "#f7f5f0",
}

# System stacks from the brand tokens.css; no webfonts, so a page opens offline
# and from a game host.
SANS = '-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif'
MONO = 'ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace'

# One scale, used by every page: body, code, section heading, page heading, and
# the one display figure (the health grade).
SCALE: dict[str, str] = {
    "body": "14px",
    "code": "12.5px",
    "h2": "15px",
    "h1": "20px",
    "figure": "30px",
}

# Geiger tile geometry (brand/tiles/geiger.svg, Lucide "radiation", ISC). The
# colors come from TOKENS: classes in a page, substituted values in the favicon.
_TILE_PATHS = (
    '<path d="M12 12h.01"/>'
    '<path d="M14 15.4641a4 4 0 0 1-4 0L7.52786 19.74597A1 1 0 0 0 7.99303 21.16211'
    ' 10 10 0 0 0 16.00697 21.16211 1 1 0 0 0 16.47214 19.74597z"/>'
    '<path d="M16 12a4 4 0 0 0-2-3.464l2.472-4.282a1 1 0 0 1 1.46-.305 10 10 0 0 1'
    ' 4.006 6.94A1 1 0 0 1 21 12z"/>'
    '<path d="M8 12a4 4 0 0 1 2-3.464L7.528 4.254a1 1 0 0 0-1.46-.305 10 10 0 0 0'
    '-4.006 6.94A1 1 0 0 0 3 12z"/>'
)

# Inline mark for a page header. aria-hidden: the product name sits beside it.
TILE_SVG = (
    '<svg class="tile" viewBox="0 0 32 32" width="32" height="32" aria-hidden="true">'
    '<rect class="tile-ground" width="32" height="32" rx="7"/>'
    '<g class="tile-glyph" transform="translate(4 4)" fill="none" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round">' + _TILE_PATHS + "</g></svg>"
)

# The same tile as a self-contained favicon data URI.
FAVICON_HREF = "data:image/svg+xml," + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    f'<rect width="32" height="32" rx="7" fill="{TOKENS["tile"]}"/>'
    f'<g transform="translate(4 4)" fill="none" stroke="{TOKENS["paper"]}" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round">' + _TILE_PATHS + "</g></svg>",
    safe="",
)

# Header lockup every page opens with: the tile and the product name.
BRAND_MARK = f'<p class="brand">{TILE_SVG}<span>Geiger · Server APM</span></p>'


_BASE_RULES = """
*,*::before,*::after{box-sizing:border-box}
body{font:SCALE_BODY/1.5 var(--apm-sans);margin:0;padding:24px;
  background:TOKENS_BG;color:TOKENS_TEXT;-webkit-font-smoothing:antialiased}
h1{font-size:SCALE_H1;font-weight:700;margin:0 0 4px}
h2{font-size:SCALE_H2;font-weight:600;margin:0 0 .5rem}
a{color:TOKENS_LINK;text-underline-offset:2px}
a:hover{text-decoration-thickness:2px}
:focus-visible{outline:2px solid TOKENS_LINK;outline-offset:2px}
code,pre,.mono{font-family:var(--apm-mono);font-size:SCALE_CODE}
pre{white-space:pre-wrap;margin:0}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 10px;border-bottom:1px solid TOKENS_RULE;text-align:left;vertical-align:top}
th{background:TOKENS_SURFACE;font-weight:600;color:TOKENS_MUTED;font-size:SCALE_CODE;
  white-space:nowrap}
/* Measurements are read column-wise against each other, so every numeric cell
   and its header get tabular figures and right alignment. */
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.muted{color:TOKENS_MUTED}
.empty{text-align:center;color:TOKENS_MUTED}
.scroll{overflow-x:auto}
.brand{display:flex;align-items:center;gap:10px;margin:0 0 10px;color:TOKENS_MUTED;
  font-size:SCALE_CODE;font-weight:600}
.brand .tile{flex:none}
.tile-ground{fill:TOKENS_TILE}
.tile-glyph{stroke:TOKENS_PAPER}
.sr-only{position:absolute;width:1px;height:1px;margin:-1px;padding:0;border:0;
  clip-path:inset(50%);overflow:hidden;white-space:nowrap}
@media (max-width:480px){body{padding:16px}}
"""


def base_css(extra: str = "") -> str:
    """The shared stylesheet, plus the page's own rules.

    Tokens and the type scale are emitted as custom properties so a page rule
    reads `var(--apm-text)` or `var(--apm-code)` rather than a copied value.
    """
    root = (
        f"--apm-sans:{SANS};--apm-mono:{MONO};"
        + "".join(f"--apm-{name}:{value};" for name, value in TOKENS.items())
        + "".join(f"--apm-{name}:{value};" for name, value in SCALE.items())
    )
    body = _BASE_RULES
    for name, value in (*TOKENS.items(), *SCALE.items()):
        body = body.replace(f"TOKENS_{name.upper()}", f"var(--apm-{name})").replace(
            f"SCALE_{name.upper()}", value
        )
    return f":root{{color-scheme:dark;{root}}}\n" + body.strip() + "\n" + extra.strip() + "\n"


REPORT_CSS = """
.page{max-width:1200px;margin:0 auto}
header{margin-bottom:20px}
header p{margin:0 0 8px}
nav{font-size:var(--apm-code)}
td ul{margin:0;padding-left:1.1rem}
"""

# Two container ideas, not one. The tiles are a fixed 2-up summary where the
# reader scans values; every data table below is full width, separated by a
# rule instead of a box, because a table does not need a frame to be read.
DASHBOARD_CSS = """
header{background:var(--apm-surface);border-bottom:1px solid var(--apm-rule);
  padding:16px 24px;margin:-24px -24px 20px}
header .muted{font-size:var(--apm-code);margin:0 0 4px}
nav{font-size:var(--apm-code)}
.tiles{display:grid;gap:1px;background:var(--apm-rule);border:1px solid var(--apm-rule);
  grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr))}
.tile-card{background:var(--apm-bg);padding:12px 16px 16px}
.tile-card ul{margin:.25rem 0 0;padding-left:1.1rem}
.tile-card p{margin:.25rem 0}
.block{margin-top:28px;padding-top:16px;border-top:1px solid var(--apm-rule)}
.block>h2{margin-bottom:.6rem}
.grade{font-size:var(--apm-figure);font-weight:700;line-height:1.1;
  font-variant-numeric:tabular-nums}
.grade .muted{font-size:var(--apm-h1);font-weight:600}
.verdict{font-size:var(--apm-h1);font-weight:600;margin:0 0 .4rem}
.layer{display:grid;grid-template-columns:minmax(80px,140px) minmax(0,340px) minmax(4ch,auto);
  gap:.75rem;align-items:center;justify-content:start;margin:.3rem 0}
.bar{height:6px;background:var(--apm-rule)}
.fill{height:100%;background:var(--apm-accent)}
.layer .val{text-align:right;font-variant-numeric:tabular-nums}
@media (max-width:480px){header{padding:16px;margin:-16px -16px 20px}}
"""
