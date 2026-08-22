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
# tsc/oxlint run through npx pinned by TSC_VERSION/OXLINT_VERSION. The repo
# deliberately does not track package.json/node_modules (.gitignore), so the
# versions live here as the single source of truth.
# Override locally: TSC_VERSION=5.9.3 OXLINT_VERSION=1.79.0 bash scripts/lint-webui.sh
#
# Requires: node/npm (npx).

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
oxlint_version="${OXLINT_VERSION:-1.79.0}"
oxlint_standards_version="${OXLINT_STANDARDS_VERSION:-0.8.1}"
tsc_version="${TSC_VERSION:-5.9.3}"
cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/7dtd-apm/oxlint-standards"
webmod_dir="$root/bridge/ApmBridge/WebMod"

# 1. Type check (per WebMod/tsconfig.json).
npx --yes -p "typescript@$tsc_version" tsc -p "$webmod_dir/tsconfig.json" --noEmit

# 2. Lint the source with oxlint. The @rikalabs plugin is fetched into the
#    cache (no-op when the pinned version is already present) and oxlint runs
#    next to it because jsPlugins resolve relative to the config file's
#    directory; a copy of the config is placed there each run.
mkdir -p "$cache_dir"
npm install --prefix "$cache_dir" --no-audit --no-fund --no-save --no-package-lock \
  "@rikalabs/oxlint-standards@$oxlint_standards_version" >/dev/null 2>&1 || {
  echo "7dtd-apm: lint-webui: could not install @rikalabs/oxlint-standards@$oxlint_standards_version into $cache_dir (offline?)" >&2
  exit 1
}
cp "$root/.oxlintrc.jsonc" "$cache_dir/oxlintrc.jsonc"
(
  cd "$cache_dir"
  npx --yes "oxlint@$oxlint_version" --config oxlintrc.jsonc --deny-warnings "$webmod_dir/bundle.ts"
)

# 3. Freshness: the committed bundle.js must equal a fresh compilation.
#    tsc versions differ on whether they emit a leading "use strict" for this
#    classic script (both forms are equivalent), so the check strips it.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
npx --yes -p "typescript@$tsc_version" tsc -p "$webmod_dir/tsconfig.json" --outDir "$tmp" >/dev/null
if ! diff -q <(sed '1{/^"use strict";$/d}' "$tmp/bundle.js") \
             <(sed '1{/^"use strict";$/d}' "$webmod_dir/bundle.js") >/dev/null; then
  echo "7dtd-apm: lint-webui: committed bundle.js is stale (bundle.ts changed without regeneration). Run: make bridge-build" >&2
  exit 1
fi
echo "7dtd-apm: lint-webui: tsc type-check, oxlint, and bundle freshness ok"
