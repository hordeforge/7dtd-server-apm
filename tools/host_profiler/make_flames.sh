#!/usr/bin/env bash
# From stacks.folded (or perf.script), emit static SVG + Speedscope + interactive HTML.
# Usage: make_flames.sh OUTDIR [title]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUTDIR="${1:?outdir with stacks.folded or perf.script}"
TITLE="${2:-7DTD CPU flamegraph}"

FOLDED="$OUTDIR/stacks.folded"
if [[ ! -s "$FOLDED" && -s "$OUTDIR/perf.script" ]]; then
  python3 "$ROOT/tools/host_profiler/stackcollapse_perf.py" "$OUTDIR/perf.script" >"$FOLDED"
fi
if [[ ! -s "$FOLDED" ]]; then
  echo "no stacks.folded in $OUTDIR" >&2
  exit 1
fi

# Annotate with [GC]/[LOCK]/[AI]/… tags for Speedscope ↔ mod mapping
ANNOTATED="$OUTDIR/stacks.annotated.folded"
python3 "$ROOT/tools/host_profiler/annotate_stacks.py" "$FOLDED" -o "$ANNOTATED" || cp -f "$FOLDED" "$ANNOTATED"
FLAME_SRC="$ANNOTATED"
[[ -s "$FLAME_SRC" ]] || FLAME_SRC="$FOLDED"

python3 "$ROOT/tools/host_profiler/flamegraph.py" "$FLAME_SRC" "$OUTDIR/flame.svg" || true
python3 "$ROOT/tools/host_profiler/folded_to_speedscope.py" "$FLAME_SRC" \
  -o "$OUTDIR/profile.speedscope.json" \
  --name "$TITLE" \
  --tree "$OUTDIR/flame.tree.json"
# Also keep raw (unannotated) speedscope for pure symbol work
python3 "$ROOT/tools/host_profiler/folded_to_speedscope.py" "$FOLDED" \
  -o "$OUTDIR/profile.raw.speedscope.json" \
  --name "$TITLE (raw)" \
  --tree "$OUTDIR/flame.raw.tree.json" 2>/dev/null || true
python3 "$ROOT/tools/host_profiler/interactive_flame.py" "$FLAME_SRC" \
  -o "$OUTDIR/flame.html" \
  --title "$TITLE (annotated)" \
  --speedscope-name "profile.speedscope.json"
python3 "$ROOT/tools/host_profiler/interactive_flame.py" "$FOLDED" \
  -o "$OUTDIR/flame.raw.html" \
  --title "$TITLE (raw)" \
  --speedscope-name "profile.raw.speedscope.json" 2>/dev/null || true

# helper launcher note
cat >"$OUTDIR/OPEN_FLAMES.txt" <<EOF
Interactive flamegraphs
-----------------------
1. flame.html          — annotated [GC]/[LOCK]/[AI]/… tags; click zoom, search
2. flame.raw.html      — unannotated native frames
3. profile.speedscope.json
     bunx speedscope profile.speedscope.json
     or drag onto https://www.speedscope.app/
4. profile.raw.speedscope.json — raw symbols
5. flame.svg           — static snapshot
6. stacks.folded / stacks.annotated.folded

Tags come from tools/host_profiler/annotate_stacks.py (catalog of native→layer labels).
Pair with csharp_bridge.md for Harmony targets.

EOF
echo "flames ready in $OUTDIR"
ls -la "$OUTDIR"/flame.html "$OUTDIR"/profile.speedscope.json "$OUTDIR"/flame.svg 2>/dev/null || true
