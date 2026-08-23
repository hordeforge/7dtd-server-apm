#!/usr/bin/env bash
# Print main PID of 7 Days to Die dedicated server, or exit 1.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/ds_paths.sh"
# Prefer exact binary path match
SRV_BIN="${SEVENDTD_DS_BIN:-}"
if [[ -z "$SRV_BIN" ]]; then
  candidate="$SEVENDTD_DS_DIR/7DaysToDieServer.x86_64"
  [[ -x "$candidate" ]] && SRV_BIN="$candidate"
fi

if [[ -n "$SRV_BIN" ]]; then
  # pgrep -f against full path can match this script; use /proc exe symlink
  while IFS= read -r pid; do
    exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
    if [[ "$exe" == "$SRV_BIN" ]]; then
      echo "$pid"
      exit 0
    fi
  done < <(pgrep -x 7DaysToDieServe 2>/dev/null || true)
fi

# Fallback: truncated comm
pid=$(pgrep -nx 7DaysToDieServe 2>/dev/null || true)
if [[ -n "${pid:-}" ]]; then
  echo "$pid"
  exit 0
fi

echo "7DaysToDieServer not running" >&2
exit 1
