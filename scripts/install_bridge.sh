#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/ds_paths.sh"
DS="$SEVENDTD_DS_DIR"
"$ROOT/scripts/build_bridge.sh"
[[ -d "$DS/Mods" ]] || { echo "ERROR: server Mods directory not found: $DS/Mods" >&2; exit 1; }
SRC="$ROOT/dist/7dtd-server-apm-bridge"
TARGET="$DS/Mods/7dtd-server-apm-bridge"
mkdir -p "$TARGET/Config"
mkdir -p "$TARGET/WebMod"
# Upgrade hygiene: build_bridge.sh rebuilds the staging tree from scratch, but
# this script copies into a mod folder that already holds a previous release.
# A file dropped from the mod (a renamed asset, a removed WebMod) would survive
# the copy and keep being served to a running server. Prune every staged file
# the new build no longer produces, exactly as the release zip does. Config/ is
# operator-owned and is never pruned.
staged="$(cd "$SRC" && find . -type f -print | LC_ALL=C sort)"
while IFS= read -r stale; do
  rel="${stale#"$TARGET"/}"
  if [[ "$rel" != Config/* ]] && ! printf '%s\n' "$staged" | grep -qxF "./$rel"; then
    rm -f "$stale"
  fi
done < <(find "$TARGET" -type f -print)
# Replace via temp+rename: cp would truncate files in place, and a running
# server may still have the old DLL mapped. Rename swaps the directory entry
# atomically so the running process keeps its old inode until restart.
install_file() {
  local src="$1" dst="$2"
  local tmp="$dst.tmp.$$"
  # A failed cp (disk full, unreadable source) trips set -e and would strand
  # this partial copy beside the live mod files forever - each failed install
  # leaves another .tmp.$$, so remove it before aborting.
  if ! cp "$src" "$tmp"; then
    rm -f "$tmp"
    exit 1
  fi
  mv -f "$tmp" "$dst"
}
install_file "$SRC/7dtd-server-apm-bridge.dll" "$TARGET/7dtd-server-apm-bridge.dll"
install_file "$SRC/ModInfo.xml" "$TARGET/ModInfo.xml"
install_file "$SRC/WebMod/bundle.js" "$TARGET/WebMod/bundle.js"
install_file "$SRC/WebMod/styling.css" "$TARGET/WebMod/styling.css"
# First install seeds the live config from the shipped example; upgrades keep
# whatever the operator tuned (the zip ships only the .example name for this
# reason). The mod falls back to built-in defaults when no config exists.
if [[ ! -f "$TARGET/Config/apmbridge.json" ]]; then
  cp "$SRC/Config/apmbridge.json.example" "$TARGET/Config/apmbridge.json"
fi
echo "OK installed -> $TARGET (existing config preserved)"
echo "Restart the dedicated server to load the new bridge DLL."
