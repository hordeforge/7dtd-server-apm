"""The APM web design tokens and the base stylesheet every generated page uses.

Report, dashboard, session index, flame delta, and the interactive flamegraph
are five views of one product. They used to carry five copies of the same
palette as raw hex literals, and they had already drifted: the dashboard alone
had a 10px card radius and a 14px body, the index and flame pages fell back to
the 16px UA default, and the interactive flamegraph hand-copied its own :root
block. One source, rendered into every page, is what keeps a reader from seeing
five different products.

The look is deliberately plain: hairline rules separate, surfaces stay flat, and
the only saturated colors are the amber accent and the ok/bad status pair. This
is an evidence tool; a value you can read at a glance beats decoration, and
nothing here trades contrast for style.
"""

from __future__ import annotations

TOKENS: dict[str, str] = {
    # Surfaces: a cool near-black page, one step up for panels, hairlines
    # between them. No gradients, no glow, no shadow.
    "bg": "#0f1115",
    "surface": "#161a22",
    "outline": "#2a2f3a",
    "rule": "#303642",
    # Text: primary, secondary, and the amber accent that marks a budgeted
    # number. Status: green within budget, red past it. There is no separate
    # near-limit tone, so a bar and the number beside it cannot disagree.
    "text": "#e8eaed",
    "muted": "#9aa0a6",
    "link": "#8ab4f8",
    "accent": "#e6bd3a",
    "ok": "#57d977",
    "bad": "#ff7070",
}

# One scale, used by every page: body, code, section heading, page heading, and
# the one display figure (the health grade). The steps are deliberately close
# (1.5px from body to code) so inline text never breaks the page rhythm, and
# the two display sizes are far enough out to read as headings on sight.
SCALE: dict[str, str] = {
    "body": "14px",
    "code": "12.5px",
    "h2": "15px",
    "h1": "20px",
    "figure": "30px",
}

_BASE_RULES = """
*,*::before,*::after{box-sizing:border-box}
body{font:SCALE_BODY/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;
  padding:24px;background:TOKENS_BG;color:TOKENS_TEXT;-webkit-font-smoothing:antialiased}
h1{font-size:SCALE_H1;font-weight:600;letter-spacing:-.01em;margin:0 0 2px}
h2{font-size:SCALE_H2;font-weight:600;margin:0 0 .5rem}
a{color:TOKENS_LINK}
a:focus-visible{outline:2px solid TOKENS_LINK;outline-offset:1px}
code,pre,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:SCALE_CODE}
pre{white-space:pre-wrap;margin:0}
table{width:100%;border-collapse:collapse}
th,td{padding:7px 8px;border-bottom:1px solid TOKENS_RULE;text-align:left;vertical-align:top}
th{background:TOKENS_SURFACE;font-weight:600;color:TOKENS_MUTED;font-size:SCALE_CODE;
  letter-spacing:.04em;text-transform:uppercase}
/* Measurements are read column-wise against each other, so every numeric cell
   gets tabular figures and right alignment. */
td.num{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:TOKENS_MUTED}
.empty{text-align:center;color:TOKENS_MUTED}
.scroll{overflow-x:auto}
.sr-only{position:absolute;width:1px;height:1px;margin:-1px;padding:0;border:0;
  clip-path:inset(50%);overflow:hidden;white-space:nowrap}
"""


def base_css(extra: str = "") -> str:
    """The shared stylesheet, plus the page's own rules.

    Tokens are emitted as custom properties so a page rule reads
    `var(--apm-text)` rather than a hex copied from this file.
    """
    root = "".join(f"--apm-{name}:{value};" for name, value in TOKENS.items())
    body = _BASE_RULES
    for name, value in (*TOKENS.items(), *SCALE.items()):
        body = body.replace(f"TOKENS_{name.upper()}", f"var(--apm-{name})").replace(
            f"SCALE_{name.upper()}", value
        )
    return f":root{{color-scheme:dark;{root}}}\n" + body.strip() + "\n" + extra.strip() + "\n"


REPORT_CSS = """
.page{max-width:1200px;margin:0 auto}
header{margin-bottom:16px}
header p{margin:0 0 8px}
nav{font-size:13px}
td ul{margin:0;padding-left:1.1rem}
"""

# Two container ideas, not one. The tiles are a fixed 2-up summary where the
# reader scans values; every data table below is full width, separated by a
# rule instead of a box, because a table does not need a frame to be read.
DASHBOARD_CSS = """
header{background:var(--apm-surface);border-bottom:1px solid var(--apm-outline);
  padding:16px 24px;margin:-24px -24px 20px}
header .muted{font-size:13px;margin:0 0 4px}
nav{font-size:13px}
.tiles{display:grid;gap:1px;background:var(--apm-rule);
  grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.tile{background:var(--apm-bg);padding:12px 16px 16px}
.tile ul{margin:.25rem 0 0;padding-left:1.1rem}
.tile p{margin:.25rem 0}
.block{margin-top:28px;padding-top:16px;border-top:1px solid var(--apm-rule)}
.block>h2{margin-bottom:.6rem}
.grade{font-size:var(--apm-figure);font-weight:600;line-height:1.1;
  font-variant-numeric:tabular-nums}
.verdict{font-size:18px;margin:0 0 .4rem}
.bar{height:6px;background:var(--apm-outline);max-width:340px;margin:.2rem 0 .6rem}
.fill{height:100%;background:var(--apm-accent)}
.layer{display:grid;grid-template-columns:minmax(90px,140px) 1fr 4ch;
  gap:.5rem;align-items:center;margin:.25rem 0}
.layer .val{text-align:right;font-variant-numeric:tabular-nums}
"""
