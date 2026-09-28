# Sourced fragment (no shebang by design): select the .NET SDK the release DLL
# is compiled with.
#
# global.json is the only place the SDK version is written, and `dotnet` reads
# it from the working directory: run from elsewhere the pin does not apply, and
# even from the root the muxer only warns when the pin is unmet, so a host with
# a different SDK silently compiles the shipped DLL with a compiler the pin
# does not name. This fragment picks the first candidate whose resolved SDK
# satisfies the pin under its rollForward (latestPatch: same feature band, patch
# at or above the pin) and fails loud when none does.
#
# Candidates, in order: an explicit DOTNET_ROOT, the dotnet on PATH, then the
# two conventional per-user install roots. A candidate that does not match is
# skipped, not fatal, so a stale private SDK cannot break a release build that
# another installed SDK can satisfy.
# shellcheck shell=bash

# The pinned SDK version from global.json, or nothing when the file is absent or
# carries no version. With no pin there is nothing to enforce, so any SDK that
# runs is accepted.
dotnet_pinned_sdk_version() {
  local repo_root="$1"
  [[ -f "$repo_root/global.json" ]] || return 0
  sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' \
    "$repo_root/global.json" | head -n1
}

# $1 resolved SDK version, $2 pinned version. global.json's rollForward is
# latestPatch, so a different feature band is a different pin, not a newer
# patch.
dotnet_sdk_matches_pin() {
  local resolved="$1" pinned="$2"
  [[ -n "$resolved" && -n "$pinned" ]] || return 0
  [[ "${resolved%.*}" == "${pinned%.*}" ]] || return 1
  local resolved_patch="${resolved##*.}" pinned_patch="${pinned##*.}"
  [[ "$resolved_patch" =~ ^[0-9]+$ && "$pinned_patch" =~ ^[0-9]+$ ]] || return 1
  (( resolved_patch >= pinned_patch ))
}

# Put a matching dotnet first on PATH and point DOTNET_ROOT at it. Must be
# called after ROOT is known and before any dotnet invocation.
dotnet_use_pinned_sdk() {
  local repo_root="$1"
  local pinned candidates=() candidate resolved home sdk_root
  pinned="$(dotnet_pinned_sdk_version "$repo_root")"
  home="${HOME:-}"

  [[ -n "${DOTNET_ROOT:-}" && -x "${DOTNET_ROOT}/dotnet" ]] && candidates+=("$DOTNET_ROOT/dotnet")
  command -v dotnet >/dev/null 2>&1 && candidates+=("$(command -v dotnet)")
  [[ -n "$home" && -x "$home/.cache/dotnet-sdk/dotnet" ]] && candidates+=("$home/.cache/dotnet-sdk/dotnet")
  [[ -n "$home" && -x "$home/.dotnet/dotnet" ]] && candidates+=("$home/.dotnet/dotnet")

  if [[ ${#candidates[@]} -eq 0 ]]; then
    echo "ERROR: .NET SDK not found; install SDK $pinned from https://dotnet.microsoft.com/download" >&2
    return 1
  fi

  for candidate in "${candidates[@]}"; do
    # `dotnet --version` resolves global.json from the working directory, so
    # ask from the repository root; that is the same answer `dotnet build` will
    # give once the build runs from there.
    resolved="$(cd "$repo_root" && "$candidate" --version 2>/dev/null || true)"
    if [[ -n "$resolved" ]] && dotnet_sdk_matches_pin "$resolved" "$pinned"; then
      sdk_root="$(dirname "$candidate")"
      export DOTNET_ROOT="$sdk_root"
      export PATH="$sdk_root:$PATH"
      if [[ -n "$pinned" ]]; then
        echo "dotnet: SDK $resolved (global.json pins $pinned, rollForward latestPatch)"
      else
        echo "dotnet: SDK $resolved (no pin in global.json)"
      fi
      return 0
    fi
    [[ -n "$pinned" ]] && echo "dotnet: skipping $candidate (SDK ${resolved:-none}, pin is $pinned)" >&2
  done

  echo "ERROR: no installed .NET SDK satisfies the global.json pin $pinned." >&2
  echo "dotnet: install SDK $pinned, or change the pin in global.json deliberately." >&2
  return 1
}
