#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/tool_versions.sh"
if [[ -n "${DOTNET_ROOT:-}" && -x "${DOTNET_ROOT}/dotnet" ]]; then
  export PATH="$DOTNET_ROOT:$PATH"
elif [[ -x "$HOME/.cache/dotnet-sdk/dotnet" ]]; then
  export DOTNET_ROOT="$HOME/.cache/dotnet-sdk"
  export PATH="$DOTNET_ROOT:$PATH"
elif [[ -x "$HOME/.dotnet/dotnet" ]]; then
  export DOTNET_ROOT="$HOME/.dotnet"
  export PATH="$DOTNET_ROOT:$PATH"
fi
dotnet --list-sdks 2>/dev/null | grep -q . || { echo "ERROR: .NET SDK not found" >&2; exit 1; }
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/ds_paths.sh"
DS="$SEVENDTD_DS_DIR"
CLIENT="${SEVENDTD_GAME_DIR:-$HOME/.local/share/Steam/steamapps/common/7 Days To Die}"
if [[ -f "$DS/7DaysToDieServer_Data/Managed/Assembly-CSharp.dll" ]]; then
  MANAGED="$DS/7DaysToDieServer_Data/Managed"; HARMONY="$DS/Mods/0_TFP_Harmony/0Harmony.dll"
elif [[ -f "$CLIENT/7DaysToDie_Data/Managed/Assembly-CSharp.dll" ]]; then
  MANAGED="$CLIENT/7DaysToDie_Data/Managed"; HARMONY="$CLIENT/Mods/0_TFP_Harmony/0Harmony.dll"
else
  echo "ERROR: no client or dedicated game assemblies found" >&2; exit 1
fi
[[ -f "$HARMONY" ]] || { echo "ERROR: Harmony not found: $HARMONY" >&2; exit 1; }
OUT="$ROOT/dist/7dtd-apm-bridge"
# Rebuild the output dir from scratch so dist mirrors current sources exactly;
# a stale member left by a removed or renamed file would ship in the package.
rm -rf "$OUT"
mkdir -p "$OUT/Config" "$OUT/WebMod"
dotnet build "$ROOT/bridge/ApmBridge/ApmBridge.csproj" -c Release \
  -p:GameManagedDir="$MANAGED" -p:HarmonyPath="$HARMONY" -p:BridgeOutput="$OUT/"
# WebMod: compile the TypeScript source (WebMod/bundle.ts) to bundle.js, the
# exact path the dashboard loads (/webmods/7dtd-apm-bridge/bundle.js).
command -v npx >/dev/null 2>&1 || { echo "ERROR: npx (Node.js/npm) not found; cannot build WebMod" >&2; exit 1; }
npx --yes -p "typescript@$TSC_VERSION" tsc -p "$ROOT/bridge/ApmBridge/WebMod/tsconfig.json"
cp "$ROOT/bridge/ApmBridge/ModInfo.xml" "$OUT/ModInfo.xml"
cp "$ROOT/bridge/ApmBridge/apmbridge.json" "$OUT/Config/apmbridge.json"
cp "$ROOT/bridge/ApmBridge/WebMod/bundle.js" "$OUT/WebMod/bundle.js"
cp "$ROOT/bridge/ApmBridge/WebMod/styling.css" "$OUT/WebMod/styling.css"
echo "OK bridge -> $OUT"
