#!/usr/bin/env bash
set -euo pipefail
# Pin locale and timezone so compiler diagnostics, resource ordering and any
# tool that stamps dates cannot leak the build host's settings into the DLL or
# the WebMod bundle.
export LC_ALL=C
export TZ=UTC
# Roslyn's deterministic MVID and the SDK's generated obj/* inputs are derived
# from source content and the source paths PathMap rewrites, not the clock, but
# the SDK also honors SOURCE_DATE_EPOCH for the build stamp. Default it to the
# HEAD commit time (two builds of one commit agree) and let a packager override.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$(dirname "$0")/.." log -1 --format=%ct 2>/dev/null || true)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ -n "$SOURCE_DATE_EPOCH" ]] || unset SOURCE_DATE_EPOCH
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/tool_versions.sh"
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/home.sh"
# global.json names the SDK the release DLL is compiled with; the muxer only
# warns when the pin is unmet, so select a matching SDK explicitly and fail loud
# when none is installed.
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/dotnet_sdk.sh"
dotnet_use_pinned_sdk "$ROOT" || exit 1
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/ds_paths.sh"
DS="$SEVENDTD_DS_DIR"
# The home fallback is taken only when no override was given, so a game install
# named by SEVENDTD_GAME_DIR stays reachable on a host that exports no HOME.
if [[ -z "${SEVENDTD_GAME_DIR:-}" ]]; then
  CLIENT="$(apm_home_or_die)/.local/share/Steam/steamapps/common/7 Days To Die" || exit 1
else
  CLIENT="$SEVENDTD_GAME_DIR"
fi
if [[ -f "$DS/7DaysToDieServer_Data/Managed/Assembly-CSharp.dll" ]]; then
  MANAGED="$DS/7DaysToDieServer_Data/Managed"; HARMONY="$DS/Mods/0_TFP_Harmony/0Harmony.dll"
elif [[ -f "$CLIENT/7DaysToDie_Data/Managed/Assembly-CSharp.dll" ]]; then
  MANAGED="$CLIENT/7DaysToDie_Data/Managed"; HARMONY="$CLIENT/Mods/0_TFP_Harmony/0Harmony.dll"
else
  echo "ERROR: no client or dedicated game assemblies found" >&2; exit 1
fi
[[ -f "$HARMONY" ]] || { echo "ERROR: Harmony not found: $HARMONY" >&2; exit 1; }
# The web server's HTTP types, referenced by the GET /api/apm handler. Named
# here so a game install without them fails with the path, not as a CS0012
# about an assembly the reader has never heard of.
[[ -f "$MANAGED/SpaceWizards_HttpListener.dll" ]] || {
  echo "ERROR: SpaceWizards_HttpListener.dll not found: $MANAGED" >&2; exit 1
}
OUT="$ROOT/dist/7dtd-server-apm-bridge"
# Rebuild the output dir from scratch so dist mirrors current sources exactly;
# a stale member left by a removed or renamed file would ship in the package.
rm -rf "$OUT"
mkdir -p "$OUT/Config" "$OUT/WebMod"
# Run from the repository root: dotnet resolves global.json from the working
# directory, so a build started anywhere else ignores the SDK pin.
(
  cd "$ROOT" &&
    dotnet build bridge/ApmBridge/ApmBridge.csproj -c Release \
      -p:GameManagedDir="$MANAGED" -p:HarmonyPath="$HARMONY" -p:BridgeOutput="$OUT/"
)
# WebMod: compile the TypeScript source (WebMod/bundle.ts) to bundle.js, the
# exact path the dashboard loads (/webmods/7dtd-server-apm-bridge/bundle.js).
command -v bunx >/dev/null 2>&1 || { echo "ERROR: bunx (bun) not found; cannot build WebMod" >&2; exit 1; }
bunx -p "typescript@$TSC_VERSION" tsc -p "$ROOT/bridge/ApmBridge/WebMod/tsconfig.json"
cp "$ROOT/bridge/ApmBridge/ModInfo.xml" "$OUT/ModInfo.xml"
# The DLL is redistributed to operators' servers inside a zip they download,
# so the terms it is shipped under travel with it: without this file the mod
# folder is a binary carrying no license text. The mod loader ignores files it
# does not recognize, and the file is inert at runtime.
cp "$ROOT/LICENSE" "$OUT/LICENSE"
# Ship the factory settings under the .example name only: users install the
# release zip by unzipping it over Mods/, so a live Config/apmbridge.json in
# the archive would reset their tuned settings on every upgrade. The mod runs
# on built-in defaults when the config file is absent.
cp "$ROOT/bridge/ApmBridge/apmbridge.json" "$OUT/Config/apmbridge.json.example"
cp "$ROOT/bridge/ApmBridge/WebMod/bundle.js" "$OUT/WebMod/bundle.js"
cp "$ROOT/bridge/ApmBridge/WebMod/styling.css" "$OUT/WebMod/styling.css"
# Build environment record: a DLL carries neither the compiler that made it nor
# the game assemblies it was compiled against, so without this a rebuild attempt
# has nothing to match against. The two sha256sum lines are checkable with
# `sha256sum -c` on the build host. Written beside the staged tree, never into
# it, so the record cannot become mod content.
{
  printf 'dotnet_sdk: %s\n' "$(dotnet --version)"
  printf 'typescript: %s\n' "$TSC_VERSION"
  printf 'target_framework: %s\n' "$(sed -n 's:.*<TargetFramework>\(.*\)</TargetFramework>.*:\1:p' "$ROOT/bridge/ApmBridge/ApmBridge.csproj")"
  printf 'source_date_epoch: %s\n' "${SOURCE_DATE_EPOCH:-unset}"
  printf 'commit: %s\n' "$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  printf 'input: Assembly-CSharp.dll\n'
  sha256sum "$MANAGED/Assembly-CSharp.dll"
  printf 'input: 0Harmony.dll\n'
  sha256sum "$HARMONY"
  printf 'input: SpaceWizards_HttpListener.dll\n'
  sha256sum "$MANAGED/SpaceWizards_HttpListener.dll"
} >"$ROOT/dist/bridge-build-inputs.txt"
echo "OK bridge -> $OUT"
