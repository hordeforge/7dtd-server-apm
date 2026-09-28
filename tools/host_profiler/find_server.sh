#!/usr/bin/env bash
# Print main PID of 7 Days to Die dedicated server, or exit 1.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/ds_paths.sh"
# Prefer exact binary path match
SRV_BIN="${SEVENDTD_DS_BIN:-}"
BIN_EXPLICIT=0
if [[ -z "$SRV_BIN" ]]; then
  candidate="$SEVENDTD_DS_DIR/7DaysToDieServer.x86_64"
  [[ -x "$candidate" ]] && SRV_BIN="$candidate"
else
  BIN_EXPLICIT=1
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

# Fallback: truncated comm (15-char TASK_COMM_LEN limit). A server started
# through a wrapper, or from a tree other than SEVENDTD_DS_DIR, has a comm
# match but no exe match.
if [[ "$BIN_EXPLICIT" == "1" ]]; then
  # The operator named the install to profile, and no running server resolves
  # to it. Returning another install's PID would profile the wrong server, so
  # report the mismatch instead of guessing.
  echo "no running server at SEVENDTD_DS_BIN=$SRV_BIN (name match suppressed)" >&2
  exit 1
fi
pid=$(pgrep -nx 7DaysToDieServe 2>/dev/null || true)
if [[ -n "${pid:-}" ]]; then
  echo "warning: matched 7DaysToDieServe by process name only, not $SRV_BIN" >&2
  echo "$pid"
  exit 0
fi

echo "7DaysToDieServer not running" >&2
exit 1
