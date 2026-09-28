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
# The files this release ships, relative to SRC. Both the prune and the install
# work off this one list, so a build that produced nothing (or stopped halfway)
# cannot be mistaken for a release that legitimately dropped files.
staged=()
while IFS= read -r rel; do
  staged+=("${rel#./}")
done < <(cd "$SRC" && find . -type f -print | LC_ALL=C sort)
[[ ${#staged[@]} -gt 0 ]] || { echo "ERROR: bridge build produced no files under $SRC" >&2; exit 1; }
for rel in "${staged[@]}"; do
  [[ -r "$SRC/$rel" ]] || { echo "ERROR: staged file unreadable: $SRC/$rel" >&2; exit 1; }
done
# Rollback: the previous release's non-config files, held until the install
# finishes. Taken before the prune, so a failure after the prune still has the
# dropped file to put back: a mod folder left half-written is worse than the
# one it replaced, because the next server start loads it as-is.
BACKUP="$(mkdir -p "$ROOT/.scratch" && mktemp -d -p "$ROOT/.scratch")"
trap 'rm -rf "$BACKUP"' EXIT
while IFS= read -r rel; do
  mkdir -p "$BACKUP/$(dirname "$rel")"
  cp -p "$TARGET/$rel" "$BACKUP/$rel"
done < <(cd "$TARGET" && find . -type f ! -path './Config/*' -print)
# Upgrade hygiene: build_bridge.sh rebuilds the staging tree from scratch, but
# this script copies into a mod folder that already holds a previous release.
# A file dropped from the mod (a renamed asset, a removed WebMod) would survive
# the copy and keep being served to a running server. Prune every staged file
# the new build no longer produces, exactly as the release zip does. Config/ is
# operator-owned and is never pruned.
declare -A staged_set=()
for rel in "${staged[@]}"; do
  staged_set["$rel"]=1
done
while IFS= read -r stale; do
  rel="${stale#"$TARGET"/}"
  if [[ "$rel" != Config/* && -z "${staged_set["$rel"]:-}" ]]; then
    rm -f "$stale"
  fi
done < <(find "$TARGET" -type f -print)
rollback() {
  echo "ERROR: install failed; rolling $TARGET back to the previous release" >&2
  # Drop what the failed install wrote, then restore the backup. Config/ is
  # never touched, so operator settings survive a rollback. The delete is
  # best effort: a directory the install could not write to (the usual cause of
  # the failure) still holds the original file, and the restore below overwrites
  # whatever the delete could not remove.
  (cd "$TARGET" && find . -type f ! -path './Config/*' -delete) 2>/dev/null || true
  local rel
  while IFS= read -r rel; do
    mkdir -p "$TARGET/$(dirname "$rel")"
    cp -p "$BACKUP/$rel" "$TARGET/$rel"
  done < <(cd "$BACKUP" && find . -type f -print)
}
# Replace via temp+rename: cp would truncate files in place, and a running
# server may still have the old DLL mapped. Rename swaps the directory entry
# atomically so the running process keeps its old inode until restart.
install_file() {
  local src="$1" dst="$2" tmp="$2.tmp.$$"
  # A failed cp (disk full, unreadable source) must not strand a partial copy
  # beside the live mod files: remove it and report the failure, so the caller
  # can roll the whole install back.
  if ! cp "$src" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$dst"
}
# Install the whole staged tree rather than a hand-listed subset: a new WebMod
# asset or mod file added by build_bridge.sh reaches the server without a
# second edit here, and a hand list that drifts from the staged tree is how a
# release silently ships a missing file.
for rel in "${staged[@]}"; do
  if ! install_file "$SRC/$rel" "$TARGET/$rel"; then
    echo "ERROR: could not install $rel" >&2
    rollback
    exit 1
  fi
done
# First install seeds the live config from the shipped example; upgrades keep
# whatever the operator tuned (the zip ships only the .example name for this
# reason). The mod falls back to built-in defaults when no config exists.
if [[ ! -f "$TARGET/Config/apmbridge.json" ]]; then
  cp "$SRC/Config/apmbridge.json.example" "$TARGET/Config/apmbridge.json"
  echo "OK installed -> $TARGET (config seeded from the shipped example)"
else
  echo "OK installed -> $TARGET (existing config preserved)"
fi
echo "Restart the dedicated server to load the new bridge DLL."
