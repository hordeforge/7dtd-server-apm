#!/usr/bin/env bash
# Compile/probe check for every bpftrace collector script.
#
# Preprocesses each .bt with placeholder values, then runs
# `bpftrace --dry-run` (attach probes, execute nothing). Requires root for
# probe attachment; skips visibly when bpftrace or sudo -n is unavailable so
# the rest of `make check` stays usable on hosts without eBPF tooling.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PRE="$ROOT/tools/host_profiler/preprocess_bt.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if ! command -v bpftrace >/dev/null; then
  echo "SKIP: bpftrace not installed; probe checks not run" >&2
  exit 0
fi
if ! sudo -n true 2>/dev/null; then
  echo "SKIP: sudo -n unavailable; bpftrace needs root for probe checks" >&2
  exit 0
fi

# Mono uprobe scripts need a real libmonobdwgc to resolve symbols.
MONO_SO="${CHECK_BT_MONO_SO:-}"
if [[ -z "$MONO_SO" ]]; then
  MONO_SO="$(find "${SEVENDTD_DS_DIR:-$HOME/.local/share/Steam/steamapps/common/7 Days to Die Dedicated Server}" \
    -name 'libmonobdwgc-2.0.so' -print -quit 2>/dev/null || true)"
fi
if [[ "$MONO_SO" == *" "* ]]; then
  # bpftrace cannot parse spaces in uprobe paths; use a space-free copy.
  cp "$MONO_SO" "$WORK/libmonobdwgc-2.0.so"
  MONO_SO="$WORK/libmonobdwgc-2.0.so"
fi

fail=0
while IFS= read -r script; do
  name="$(basename "$script" .bt)"
  prepared="$WORK/$name.bt"
  if grep -q 'MONO_SO:' "$script" && [[ -z "$MONO_SO" ]]; then
    echo "SKIP $script (no libmonobdwgc-2.0.so found; set CHECK_BT_MONO_SO)"
    continue
  fi
  python3 "$PRE" "$script" -o "$prepared" --pid 1 --comm check --mono-so "$MONO_SO"
  if timeout 60 sudo -n bpftrace --dry-run "$prepared" >/dev/null 2>"$WORK/$name.err"; then
    echo "ok   $script"
  else
    echo "FAIL $script"
    sed 's/^/     /' "$WORK/$name.err"
    fail=1
  fi
done < <(find "$ROOT/tools/apm/collectors" "$ROOT/tools/host_profiler/scripts" -name '*.bt' | sort)

exit "$fail"
