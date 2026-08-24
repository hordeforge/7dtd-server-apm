#!/usr/bin/env bash
# perf record/report for 7DTD dedicated (user stacks; no root required for :u events often).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUTDIR="${1:-}"
SECONDS_N="${2:-30}"
PID="${3:-}"

if [[ -z "$PID" ]]; then
  PID="$("$ROOT/tools/host_profiler/find_server.sh")"
fi
if [[ -z "$OUTDIR" ]]; then
  # Never write captures into the git tree (see README); default to the shared
  # APM data dir alongside sessions.
  OUTDIR="${SEVENDTD_APM_DIR:-$HOME/.local/share/7dtd-server-apm}/perf_$(date -u +%Y%m%d_%H%M%S)"
fi
mkdir -p "$OUTDIR"

PERF_MAP="/tmp/perf-$PID.map"
if [[ ! -s "$PERF_MAP" ]]; then
  echo "WARNING: $PERF_MAP missing; managed Mono frames will be [jit]. Run 'apm jitmap' via the bridge (scenario run captures do this automatically; bare capture needs --symbolize)." >&2
fi

echo "perf record pid=$PID seconds=$SECONDS_N -> $OUTDIR"
# fp unwinding: Mono JIT keeps frame pointers; measured A/B on this workload
# gave fp 136k resolvable frames vs dwarf 3.5k (dwarf cannot unwind through
# JIT code and bloats perf.data ~100x). 99 Hz keeps files sane under load.
# timeout must outlive the inner sleep so perf flushes its data cleanly.
set +e
timeout $((SECONDS_N + 15)) perf record -F 99 -g --call-graph fp \
  -p "$PID" -o "$OUTDIR/perf.data" -- sleep "$SECONDS_N" 2>"$OUTDIR/perf_record.err"
RC=$?
echo "perf record exit=$RC"
rm -f "$OUTDIR/perf.data.old"
set -e

perf report -i "$OUTDIR/perf.data" --stdio --no-children -n --percent-limit 0.5 \
  >"$OUTDIR/perf_report.txt" 2>"$OUTDIR/perf_report.err" || true
perf report -i "$OUTDIR/perf.data" --stdio --children -n --percent-limit 1 \
  >"$OUTDIR/perf_report_children.txt" 2>/dev/null || true
perf script -i "$OUTDIR/perf.data" >"$OUTDIR/perf.script" 2>"$OUTDIR/perf_script.err" || true

# Preserve the exact managed address map used by this process/capture.
# The mtime of /proc/PID equals the process start: a map older than that
# belongs to a dead previous occupant of this reused pid and would silently
# misresolve every managed frame during symbol annotation, so it must not be
# copied into the session (those frames honestly stay [jit] instead).
MAP_FRESH=0
if [[ -s "$PERF_MAP" ]]; then
  if [[ -d "/proc/$PID" ]]; then
    proc_start="$(stat -c %Y "/proc/$PID" 2>/dev/null || true)"
    map_mtime="$(stat -c %Y "$PERF_MAP" 2>/dev/null || true)"
    if [[ -n "$proc_start" && -n "$map_mtime" && "$map_mtime" -ge "$proc_start" ]]; then
      MAP_FRESH=1
    else
      echo "WARNING: $PERF_MAP predates pid $PID (stale map from a reused pid); not copying it; managed frames stay [jit]. Re-run 'apm jitmap'." >&2
    fi
  else
    # Target already exited after recording; its surviving map is the best
    # evidence for the data just captured and cannot be revalidated.
    MAP_FRESH=1
  fi
fi
if ((MAP_FRESH)); then
  cp "$PERF_MAP" "$OUTDIR/perf-$PID.map"
fi

# folded stacks + static SVG + Speedscope + interactive HTML
if [[ -s "$OUTDIR/perf.script" ]]; then
  python3 "$ROOT/tools/host_profiler/stackcollapse_perf.py" "$OUTDIR/perf.script" >"$OUTDIR/stacks.folded" || true
  if [[ -s "$OUTDIR/stacks.folded" ]]; then
    chmod +x "$ROOT/tools/host_profiler/make_flames.sh"
    "$ROOT/tools/host_profiler/make_flames.sh" "$OUTDIR" "7DTD pid=$PID CPU" || true
  fi
fi

# Main-thread-only folded stacks: tid==pid is the Unity sim thread that runs the
# 20 TPS tick, so this ranks hot paths FOR THE TICK ITSELF (what gates ms_per_tick),
# distinct from the aggregate all-thread stacks.folded above.
perf script -i "$OUTDIR/perf.data" --tid="$PID" >"$OUTDIR/perf.main.script" 2>/dev/null || true
if [[ -s "$OUTDIR/perf.main.script" ]]; then
  python3 "$ROOT/tools/host_profiler/stackcollapse_perf.py" "$OUTDIR/perf.main.script" >"$OUTDIR/stacks.main.folded" || true
fi

echo "done: $OUTDIR"
ls -la "$OUTDIR"
if [[ -f "$OUTDIR/flame.html" ]]; then
  echo "interactive: $OUTDIR/flame.html"
  echo "speedscope:  $OUTDIR/profile.speedscope.json  (npx speedscope …)"
fi
