# Sourced fragment (no shebang by design): shared pinned tool versions for the
# WebMod so its two consumers cannot disagree about which TypeScript compiles
# the shipped bundle.js: build_bridge.sh (release artifact) and lint-webui.sh
# (freshness gate). An explicit environment override always wins.
# shellcheck shell=bash
: "${TSC_VERSION:=5.9.3}"
export TSC_VERSION
