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
# Replace via temp+rename: cp would truncate files in place, and a running
# server may still have the old DLL mapped. Rename swaps the directory entry
# atomically so the running process keeps its old inode until restart.
install_file() {
  local src="$1" dst="$2"
  local tmp="$dst.tmp.$$"
  cp "$src" "$tmp"
  mv -f "$tmp" "$dst"
}
install_file "$ROOT/dist/7dtd-apm-bridge/7dtd-apm-bridge.dll" "$TARGET/7dtd-apm-bridge.dll"
install_file "$ROOT/dist/7dtd-apm-bridge/ModInfo.xml" "$TARGET/ModInfo.xml"
install_file "$ROOT/dist/7dtd-apm-bridge/WebMod/bundle.js" "$TARGET/WebMod/bundle.js"
install_file "$ROOT/dist/7dtd-apm-bridge/WebMod/styling.css" "$TARGET/WebMod/styling.css"
if [[ ! -f "$TARGET/Config/apmbridge.json" ]]; then
  cp "$ROOT/dist/7dtd-apm-bridge/Config/apmbridge.json" "$TARGET/Config/"
fi
echo "OK installed -> $TARGET (existing config preserved)"
echo "Restart the dedicated server to load the new bridge DLL."
