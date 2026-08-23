#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/ds_paths.sh"
DS="$SEVENDTD_DS_DIR"
"$ROOT/scripts/build_bridge.sh"
[[ -d "$DS/Mods" ]] || { echo "ERROR: server Mods directory not found: $DS/Mods" >&2; exit 1; }
TARGET="$DS/Mods/7dtd-apm-bridge"
mkdir -p "$TARGET/Config"
mkdir -p "$TARGET/WebMod"
cp "$ROOT/dist/7dtd-apm-bridge/7dtd-apm-bridge.dll" "$TARGET/"
cp "$ROOT/dist/7dtd-apm-bridge/ModInfo.xml" "$TARGET/"
cp "$ROOT/dist/7dtd-apm-bridge/WebMod/bundle.js" "$TARGET/WebMod/"
cp "$ROOT/dist/7dtd-apm-bridge/WebMod/styling.css" "$TARGET/WebMod/"
if [[ ! -f "$TARGET/Config/apmbridge.json" ]]; then
  cp "$ROOT/dist/7dtd-apm-bridge/Config/apmbridge.json" "$TARGET/Config/"
fi
echo "OK installed -> $TARGET (existing config preserved)"
