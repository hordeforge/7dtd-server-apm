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
DESCRIBE="$(git -C "$ROOT" describe --tags --always 2>/dev/null || true)"
VERSION="${VERSION:-$DESCRIBE}"
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
STAGE="$(mktemp -d)"
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
echo "Packaged -> $OUT (+ .sha256)"
