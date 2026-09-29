#!/usr/bin/env bash
# From stacks.folded (or perf.script), emit Speedscope + interactive HTML.
# Usage: make_flames.sh OUTDIR [title]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/python.sh"
OUTDIR="${1:?outdir with stacks.folded or perf.script}"
TITLE="${2:-Geiger CPU flamegraph}"

FOLDED="$OUTDIR/stacks.folded"
if [[ ! -s "$FOLDED" && -s "$OUTDIR/perf.script" ]]; then
  "$SEVENDTD_APM_PYTHON" "$ROOT/tools/host_profiler/stackcollapse_perf.py" "$OUTDIR/perf.script" >"$FOLDED"
fi
if [[ ! -s "$FOLDED" ]]; then
  echo "no stacks.folded in $OUTDIR" >&2
  exit 1
fi

# Annotate with [GC]/[LOCK]/[AI]/… tags for Speedscope ↔ mod mapping
ANNOTATED="$OUTDIR/stacks.annotated.folded"
"$SEVENDTD_APM_PYTHON" "$ROOT/tools/host_profiler/annotate_stacks.py" "$FOLDED" -o "$ANNOTATED" || cp -f "$FOLDED" "$ANNOTATED"
FLAME_SRC="$ANNOTATED"
[[ -s "$FLAME_SRC" ]] || FLAME_SRC="$FOLDED"

"$SEVENDTD_APM_PYTHON" "$ROOT/tools/host_profiler/folded_to_speedscope.py" "$FLAME_SRC" \
  -o "$OUTDIR/profile.speedscope.json" \
  --name "$TITLE" \
  --tree "$OUTDIR/flame.tree.json"
# Also keep raw (unannotated) speedscope for pure symbol work
"$SEVENDTD_APM_PYTHON" "$ROOT/tools/host_profiler/folded_to_speedscope.py" "$FOLDED" \
  -o "$OUTDIR/profile.raw.speedscope.json" \
  --name "$TITLE (raw)" \
  --tree "$OUTDIR/flame.raw.tree.json" 2>/dev/null || true
# The HTML builder reuses the d3 tree folded_to_speedscope.py already wrote
# instead of re-folding and re-aggregating the same stacks; the raw tree is
# best-effort, so fall back to the folded input when it is missing.
# Reuse is only safe while the tree is no older than the folded file it was
# derived from: a build that dies between rewriting stacks.folded and rewriting
# the tree would otherwise embed the PREVIOUS run's tree in a page filed beside
# the new folded stacks, and nothing in the output would show the mismatch.
# `-nt` holds in the normal pass (the tree is written after its input) and
# fails closed onto the folded fallback, which is always derived from the file
# in hand.
emit_flame() {
  local tree="$1" folded="$2" out="$3" title="$4" scope_name="$5"
  if [[ -s "$tree" && "$tree" -nt "$folded" ]]; then
    "$SEVENDTD_APM_PYTHON" "$ROOT/tools/host_profiler/interactive_flame.py" --tree "$tree" \
      -o "$out" --title "$title" --speedscope-name "$scope_name"
  else
    "$SEVENDTD_APM_PYTHON" "$ROOT/tools/host_profiler/interactive_flame.py" "$folded" \
      -o "$out" --title "$title" --speedscope-name "$scope_name"
  fi
}
emit_flame "$OUTDIR/flame.tree.json" "$FLAME_SRC" "$OUTDIR/flame.html" \
  "$TITLE (annotated)" "profile.speedscope.json"
emit_flame "$OUTDIR/flame.raw.tree.json" "$FOLDED" "$OUTDIR/flame.raw.html" \
  "$TITLE (raw)" "profile.raw.speedscope.json" 2>/dev/null || true

# Completion marker. The capture driver re-runs this script when a previous
# pass did not finish; without it a partially written flame set is
# indistinguishable from a complete one.
: >"$OUTDIR/flames.done"

# helper launcher note
cat >"$OUTDIR/OPEN_FLAMES.txt" <<EOF
Interactive flamegraphs
-----------------------
1. flame.html          : annotated [GC]/[LOCK]/[AI]/… tags; click zoom, search
2. flame.raw.html      : unannotated native frames
3. profile.speedscope.json
     bunx speedscope profile.speedscope.json
     or drag onto https://www.speedscope.app/
4. profile.raw.speedscope.json: raw symbols
5. stacks.folded / stacks.annotated.folded

Tags come from tools/host_profiler/annotate_stacks.py (catalog of native→layer labels).
Pair with docs/APM_CS_BRIDGE.md for Harmony targets.

EOF
echo "flames ready in $OUTDIR"
ls -la "$OUTDIR"/flame.html "$OUTDIR"/profile.speedscope.json 2>/dev/null || true
