#!/usr/bin/env bash
# Compatibility launcher: capture orchestration lives in the Python CLI.
# Usage mirrors `7dtd-apm capture`:
#   ./tools/apm/capture.sh --seconds 45
#   ./tools/apm/capture.sh --seconds 60 --no-app
#   ./tools/apm/capture.sh --only hw,threads,futex,cpu
#   ./tools/apm/capture.sh --pid 12345 --telnet-port 8081
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \?//'; exit 0 ;;
    --seconds|-s) ARGS+=(--seconds "$2"); shift 2 ;;
    --only) ARGS+=(--only "$2"); shift 2 ;;
    --pid) ARGS+=(--pid "$2"); shift 2 ;;
    --telnet-host) ARGS+=(--telnet-host "$2"); shift 2 ;;
    --telnet-port) ARGS+=(--telnet-port "$2"); shift 2 ;;
    --telnet-pass) echo "--telnet-pass was removed; use SEVENDTD_TELNET_PASSWORD" >&2; exit 2 ;;
    --comm) shift 2 ;; # comm is fixed in the orchestrator
    --no-app) ARGS+=(--no-app); shift ;;
    *) echo "unknown $1" >&2; exit 2 ;;
  esac
done

exec env PYTHONPATH="$ROOT/tools" python3 -m apm_suite.cli capture "${ARGS[@]}"
