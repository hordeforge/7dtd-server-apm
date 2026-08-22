#!/usr/bin/env bash
# Build the bridge and package dist/7dtd-apm-bridge into a distributable zip.
#
# The zip contains the 7dtd-apm-bridge/ mod folder at its top level, so
# unzipping it inside <server>/Mods installs the mod (Mods/7dtd-apm-bridge/).
#
# Version: taken from the newest git tag (vX.Y.Z -> X.Y.Z), or overridden
# with VERSION=x.y.z. Requires a local game install: build_bridge.sh compiles
# against the shipped Assembly-CSharp.dll, which this repo does not
# redistribute (see ../MODDING_BEST_PRACTICES.md / AGENTS.md). Same pattern as
# ../7dtd-optimizer/scripts/package.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

"$ROOT/scripts/build_bridge.sh"

VERSION="${VERSION:-$(git -C "$ROOT" describe --tags --always 2>/dev/null || true)}"
VERSION="${VERSION#v}"
if [[ -z "$VERSION" || "$VERSION" == *-* ]]; then
  # No tag yet (or dirty/untagged describe): fall back to a short commit id.
  VERSION="$(git -C "$ROOT" rev-parse --short HEAD)"
fi

OUT="$ROOT/dist/7dtd-apm-bridge-$VERSION.zip"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -a "$ROOT/dist/7dtd-apm-bridge" "$STAGE/"
( cd "$STAGE" && zip -qr "$OUT" 7dtd-apm-bridge )
echo "Packaged -> $OUT"
