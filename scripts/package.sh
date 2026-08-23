#!/usr/bin/env bash
# Build the bridge and package dist/7dtd-apm-bridge into a distributable zip.
#
# The zip contains the 7dtd-apm-bridge/ mod folder at its top level, so
# unzipping it inside <server>/Mods installs the mod (Mods/7dtd-apm-bridge/).
#
# Version: taken from the newest git tag (vX.Y.Z -> X.Y.Z), or overridden
# with VERSION=x.y.z. A clean-tag build must match the mod version declared
# in bridge/ApmBridge/ModInfo.xml (prevents shipping a zip named after a
# stale tag); untagged/dirty builds fall back to a short commit id.
# Requires a local game install: build_bridge.sh compiles
# against the shipped Assembly-CSharp.dll, which this repo does not
# redistribute (see ../MODDING_BEST_PRACTICES.md / AGENTS.md). Same pattern as
# ../7dtd-optimizer/scripts/package.sh.
set -euo pipefail
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

OUT="$ROOT/dist/7dtd-apm-bridge-$VERSION.zip"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -a "$ROOT/dist/7dtd-apm-bridge" "$STAGE/"
# zip updates archives in place, so a rerun over an old zip would keep stale
# members that vanished from dist; rebuild the artifact from scratch instead.
rm -f "$OUT"
( cd "$STAGE" && zip -qr "$OUT" 7dtd-apm-bridge )
echo "Packaged -> $OUT"
