#!/usr/bin/env bash
# Hardware / software PMU counters for cache, IPC, stalls, faults.
# Usage: hw_perf.sh PID SECONDS OUTDIR
set -euo pipefail
PID="${1:?pid}"
SECS="${2:-30}"
OUT="${3:?outdir}"
mkdir -p "$OUT"

EVENTS=(
  cycles
  instructions
  branches
  branch-misses
  cache-references
  cache-misses
  context-switches
  cpu-migrations
  page-faults
  task-clock
  cpu-clock
)

# Optional LLC / stall events (may fail on some CPUs — try then drop)
OPTIONAL=(
  L1-dcache-load-misses
  L1-dcache-loads
  LLC-loads
  LLC-load-misses
  dTLB-load-misses
  dTLB-loads
  stalled-cycles-frontend
  stalled-cycles-backend
)

list=$(IFS=,; echo "${EVENTS[*]}")
opt_ok=()
for e in "${OPTIONAL[@]}"; do
  if perf list 2>/dev/null | grep -qE "^[[:space:]]*${e}([[:space:]]|$)"; then
    opt_ok+=("$e")
  fi
done
if ((${#opt_ok[@]})); then
  list="$list,$(IFS=,; echo "${opt_ok[*]}")"
fi

echo "perf stat events: $list" | tee "$OUT/hw_events.txt"
# Run the totals and the 1s interval series concurrently so the collector
# finishes within one capture window instead of two. The timeout must outlive
# the inner sleep: perf stat only writes -o on a clean exit, and a TERM that
# lands while it is flushing loses the whole file.
set +e
timeout $((SECS + 15)) perf stat -e "$list" -p "$PID" -o "$OUT/hw_stat.txt" -- \
  sleep "$SECS" 2>"$OUT/hw_stat.err" &
MAIN_PID=$!
timeout $((SECS + 15)) perf stat -I 1000 -e cycles,instructions,cache-misses,context-switches,page-faults \
  -p "$PID" -o "$OUT/hw_stat_interval.txt" -- sleep "$SECS" 2>"$OUT/hw_interval.err" &
INTERVAL_PID=$!
wait "$MAIN_PID"
RC=$?
wait "$INTERVAL_PID"
set -e
echo "hw_perf exit=$RC"
exit 0
