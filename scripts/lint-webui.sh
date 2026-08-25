#!/usr/bin/env bash
# Lint the WebMod TypeScript (bridge/ApmBridge/WebMod/bundle.ts) with tsc and
# oxlint against the anti-slop + strict rule set in .oxlintrc.jsonc, then check
# the committed bundle.js is fresh (a .ts edit that was not compiled fails the
# gate). Part of `make check` (target: lint-webui).
#
#   1. tsc --noEmit: the type gate (per WebMod/tsconfig.json).
#   2. oxlint over bundle.ts with the anti-slop rule set in .oxlintrc.jsonc
#      (warnings fail via --deny-warnings).
#   3. Freshness: the committed bundle.js must equal a fresh compilation, so a
#      .ts edit that was not compiled and committed fails the gate.
#
# tsc/oxlint run through bunx pinned by TSC_VERSION/OXLINT_VERSION. The repo
# deliberately does not track package.json/node_modules (.gitignore), so the
# versions live in scripts/lib/tool_versions.sh (TSC_VERSION, shared with the
# release build) and here as their single sources of truth.
# Override locally: TSC_VERSION=5.9.3 OXLINT_VERSION=1.79.0 bash scripts/lint-webui.sh
#
# The anti-slop plugin is vendored source fetched outside any registry, so its
# tarball carries no publisher integrity metadata; ANTI_SLOP_SHA pins the
# commit and ANTI_SLOP_SHA256 verifies the downloaded bytes before extraction.
# Update both pins together after inspecting the new upstream source.
#
# Requires: bun (bunx).

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$root/scripts/lib/tool_versions.sh"
oxlint_version="${OXLINT_VERSION:-1.79.0}"
oxlint_standards_version="${OXLINT_STANDARDS_VERSION:-0.8.1}"
oxlint_tsgolint_version="${OXLINT_TSGOLINT_VERSION:-7.0.2001}"
oxlint_plugins_version="${OXLINT_PLUGINS_VERSION:-1.79.0}"
anti_slop_sha="${ANTI_SLOP_SHA:-6d538555cb151d4121ed51a27db81890eacf8ae9}"
# sha256 of the pinned anti-slop tarball (see header comment): the extracted
# source is loaded as executable oxlint plugin code, so verify bytes before use.
anti_slop_sha256="${ANTI_SLOP_SHA256:-a720663fd2562e22e3da670769faa88dc34c9a761fdd9a7d285e20d92871848e}"
cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/7dtd-server-apm/oxlint-standards"
webmod_dir="$root/bridge/ApmBridge/WebMod"

command -v bunx >/dev/null 2>&1 || {
  echo "7dtd-server-apm: lint-webui: bunx (bun) not found; tsc/oxlint run through pinned bunx packages" >&2
  exit 1
}

# 1. Type check (per WebMod/tsconfig.json, strict).
bunx -p "typescript@$TSC_VERSION" tsc -p "$webmod_dir/tsconfig.json" --noEmit

# 2. Lint the source with oxlint. The @rikalabs plugin, the vendored
#    dmmulroy/anti-slop plugin source (pinned by ANTI_SLOP_SHA and
#    sha256-verified by ANTI_SLOP_SHA256; the project is
#    vendored source, not an npm package), and oxlint-tsgolint (the type-aware
#    backend, see options.typeAware in .oxlintrc.jsonc) are fetched into the
#    cache (no-op when the pinned versions are already present) and oxlint runs
#    next to them because jsPlugins resolve relative to the config file's
#    directory; a copy of the config is placed there each run. The pinned
#    packages are installed with one additive `bun add` invocation: it merges
#    the pins into the cache manifest and never prunes what a sibling script
#    installed. @oxlint/plugins is the plugin API the anti-slop source
#    imports; without it the plugin cannot load.
mkdir -p "$cache_dir"
if [ ! -d "$cache_dir/anti-slop-src" ]; then
  curl -fsSL "https://github.com/dmmulroy/anti-slop/archive/$anti_slop_sha.tar.gz" -o "$cache_dir/anti-slop.tar.gz"
  if ! printf '%s  %s\n' "$anti_slop_sha256" "$cache_dir/anti-slop.tar.gz" | sha256sum --check --status; then
    rm -f "$cache_dir/anti-slop.tar.gz"
    echo "7dtd-server-apm: lint-webui: anti-slop tarball for $anti_slop_sha does not match ANTI_SLOP_SHA256 (GitHub re-gzip or tampering); inspect upstream and update ANTI_SLOP_SHA + ANTI_SLOP_SHA256 together to accept" >&2
    exit 1
  fi
  mkdir -p "$cache_dir/anti-slop-src"
  tar xzf "$cache_dir/anti-slop.tar.gz" -C "$cache_dir/anti-slop-src" --strip-components=2 "anti-slop-$anti_slop_sha/src"
fi
# type module: the vendored anti-slop plugin source is ESM; without the field
# node reparses it with a MODULE_TYPELESS_PACKAGE_JSON warning.
[ -f "$cache_dir/package.json" ] || printf '{"type":"module"}\n' > "$cache_dir/package.json"
( cd "$cache_dir" && bun add --silent \
    "@rikalabs/oxlint-standards@$oxlint_standards_version" \
    "oxlint-tsgolint@$oxlint_tsgolint_version" \
    "@oxlint/plugins@$oxlint_plugins_version" ) >/dev/null 2>&1 || {
  echo "7dtd-server-apm: lint-webui: could not install @rikalabs/oxlint-standards@$oxlint_standards_version + oxlint-tsgolint@$oxlint_tsgolint_version + @oxlint/plugins@$oxlint_plugins_version into $cache_dir (offline?)" >&2
  exit 1
}
cp "$root/.oxlintrc.jsonc" "$cache_dir/oxlintrc.jsonc"
(
  cd "$cache_dir"
  # tsgolint is not on the user's PATH; oxlint finds it via PATH lookup.
  PATH="$cache_dir/node_modules/.bin:$PATH" \
    bunx "oxlint@$oxlint_version" --config oxlintrc.jsonc --deny-warnings "$webmod_dir/bundle.ts"
)

# 3. Freshness: the committed bundle.js must equal a fresh compilation.
#    tsc versions differ on whether they emit a leading "use strict" for this
#    classic script (both forms are equivalent), so the check strips it.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
bunx -p "typescript@$TSC_VERSION" tsc -p "$webmod_dir/tsconfig.json" --outDir "$tmp" >/dev/null
if ! diff -q <(sed '1{/^"use strict";$/d}' "$tmp/bundle.js") \
             <(sed '1{/^"use strict";$/d}' "$webmod_dir/bundle.js") >/dev/null; then
  echo "7dtd-server-apm: lint-webui: committed bundle.js is stale (bundle.ts changed without regeneration). Run: make bridge-build" >&2
  exit 1
fi
echo "7dtd-server-apm: lint-webui: tsc type-check, oxlint, and bundle freshness ok"
