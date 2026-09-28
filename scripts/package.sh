#!/usr/bin/env bash
# Build the bridge and package dist/7dtd-server-apm-bridge into a distributable zip.
#
# The zip contains the 7dtd-server-apm-bridge/ mod folder at its top level, so
# unzipping it inside <server>/Mods installs the mod (Mods/7dtd-server-apm-bridge/).
#
# Version: taken from the newest git tag (vX.Y.Z -> X.Y.Z), or overridden
# with VERSION=x.y.z. A clean-tag build must match the mod version declared
# in bridge/ApmBridge/ModInfo.xml (prevents shipping a zip named after a
# stale tag); untagged/dirty builds fall back to a short commit id.
# Requires a local game install: build_bridge.sh compiles
# against the shipped Assembly-CSharp.dll, which this repo does not
# redistribute (see ../MODDING_BEST_PRACTICES.md / AGENTS.md). Same pattern as
# ../7dtd-server-optimizer/scripts/package.sh.
set -euo pipefail
# Pin locale and timezone: zip stores member times as MS-DOS local time, so an
# unpinned TZ would leak the build host's zone into the artifact bytes.
export LC_ALL=C
export TZ=UTC
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

"$ROOT/scripts/build_bridge.sh"

MANIFEST_VERSION="$(sed -n 's/.*<Version value="\([0-9.]*\)".*/\1/p' "$ROOT/bridge/ApmBridge/ModInfo.xml" | head -n1)"
# A release zip must be the commit its name claims. `git describe` reports the
# exact tag for a tree carrying uncommitted edits, so the ModInfo.xml check
# below passes and a maintainer publishes an archive whose bytes are not the
# commit the tag names; only the .buildinfo.txt betrays it. `git status` is the
# honest test and it is strictly wider than `describe --dirty`, which ignores
# untracked files: a new untracked .cs file is globbed by the csproj and lands
# in the DLL. Ignored paths (.scratch, .uv-cache, dist) never appear here.
TREE_STATE="$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || true)"
UNCOMMITTED="$(git -C "$ROOT" status --porcelain 2>/dev/null || true)"
if [[ -n "$UNCOMMITTED" && "${ALLOW_DIRTY:-}" != 1 ]]; then
  echo "package: refusing to package a dirty working tree; the zip would not be the commit its name claims" >&2
  echo "$UNCOMMITTED" >&2
  echo "package: commit or stash the changes, or set ALLOW_DIRTY=1 for a local build you will not publish" >&2
  exit 1
fi
# The opt-out has to change the artifact name too, or a local build is
# indistinguishable from the release it sits next to. describe --dirty covers
# tracked edits only, so an untracked-only tree still describes as the exact
# tag; marking it here routes every opted-out build through the short-commit
# fallback below instead of the release name.
if [[ -n "$UNCOMMITTED" && "$TREE_STATE" != *-dirty ]]; then
  TREE_STATE="${TREE_STATE}-dirty"
fi
VERSION="${VERSION:-$TREE_STATE}"
if [[ -n "$VERSION" && "$VERSION" == v[0-9]*.[0-9]*.[0-9]* && "$VERSION" != *-* ]]; then
  # Exact-tag build: the zip name must not disagree with the packaged DLL.
  if [[ -z "$MANIFEST_VERSION" ]]; then
    echo "package: cannot read mod version from bridge/ApmBridge/ModInfo.xml" >&2
    exit 1
  fi
  if [[ "${VERSION#v}" != "$MANIFEST_VERSION" ]]; then
    echo "package: tag $VERSION does not match mod version $MANIFEST_VERSION (ModInfo.xml)" >&2
    echo "package: tag the release first, or override with VERSION=$MANIFEST_VERSION" >&2
    exit 1
  fi
fi
VERSION="${VERSION#v}"
if [[ -z "$VERSION" || "$VERSION" == *-* ]]; then
  # No tag yet (or dirty/untagged describe): fall back to a short commit id.
  VERSION="$(git -C "$ROOT" rev-parse --short HEAD)"
fi

OUT="$ROOT/dist/7dtd-server-apm-bridge-$VERSION.zip"
# Repo-local scratch instead of tmpfs: the staged mod tree plus its zip can run
# to tens of MB, and /tmp is RAM-backed on typical hosts.
STAGE="$(mkdir -p "$ROOT/.scratch" && mktemp -d -p "$ROOT/.scratch")"
trap 'rm -rf "$STAGE"' EXIT
cp -a "$ROOT/dist/7dtd-server-apm-bridge" "$STAGE/"
# Debug symbols never ship: the release zip carries the DLL, ModInfo, the
# example Config, and WebMod only. The live config name is excluded too, so a
# future staging change cannot silently reintroduce upgrade resets of user
# settings (unzipping over Mods/ overwrites every archive member).
rm -f "$STAGE"/7dtd-server-apm-bridge/*.pdb "$STAGE"/7dtd-server-apm-bridge/Config/apmbridge.json
# Reproducible archive: pin every member's mtime to a source-derived epoch,
# strip uid/gid and extended-timestamp extra fields (-X), and add members in
# LC_ALL=C sort order instead of readdir order. Without this, cp -a mtimes and
# filesystem ordering leak into the zip and no two rebuilds share a sha256,
# making the published .sha256 unverifiable. Epoch precedence per
# reproducible-builds.org: SOURCE_DATE_EPOCH, else the HEAD commit time (two
# builds of one commit agree), else wall clock (non-git tree).
EPOCH="${SOURCE_DATE_EPOCH:-}"
if [[ -z "$EPOCH" ]]; then
  EPOCH="$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || true)"
fi
[[ -n "$EPOCH" ]] || EPOCH="$(date +%s)"
find "$STAGE" -exec touch -h -d "@$EPOCH" {} +
# Normalize mode bits too: cp -a and mkdir carry the packager's umask into the
# stored unix attributes, so a zip built under umask 022 and one built under
# 0077 differ byte for byte. Nothing in the mod is executed, so 0644/0755 is
# both the least-privilege mode and a host-independent one.
find "$STAGE" -type f -exec chmod 644 {} +
find "$STAGE" -type d -exec chmod 755 {} +
command -v zip >/dev/null 2>&1 || { echo "package: zip not found; cannot build $OUT" >&2; exit 1; }
# zip updates archives in place, so a rerun over an old zip would keep stale
# members that vanished from dist; rebuild the artifact from scratch instead.
rm -f "$OUT"
(
  cd "$STAGE" &&
    find 7dtd-server-apm-bridge -mindepth 1 -print0 |
    LC_ALL=C sort -z |
    xargs -0 zip -X -q "$OUT"
)
# Release integrity: operators verify the zip before dropping it into Mods/
# (sha256sum -c). Rebuilt alongside the zip so it can never go stale.
rm -f "$OUT.sha256"
{ cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256"; }
# Toolchain and game-assembly record (build_bridge.sh writes it, and the build
# that produced the zip is the only place those facts are known). Beside the
# zip, never inside it, for the same reason as the SBOM: the archive unzips
# into <server>/Mods/, so anything extra in it installs as mod content.
[[ -f "$ROOT/dist/bridge-build-inputs.txt" ]] || {
  echo "package: missing dist/bridge-build-inputs.txt; build_bridge.sh did not complete" >&2
  exit 1
}
rm -f "$OUT.buildinfo.txt"
cp "$ROOT/dist/bridge-build-inputs.txt" "$OUT.buildinfo.txt"
# Dependency inventory beside the zip, never inside it: the archive unzips
# into <server>/Mods/, so a BOM file there would install as mod content. The
# target is the Makefile's, so the release inventory and `make sbom` cannot
# drift apart.
make -C "$ROOT" sbom
echo "Packaged -> $OUT (+ .sha256, .buildinfo.txt)"
