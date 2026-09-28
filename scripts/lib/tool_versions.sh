# Sourced fragment (no shebang by design): shared pinned tool versions for the
# build and lint scripts, so build_bridge.sh (release artifact), lint-webui.sh
# and lint-html.sh cannot disagree about which compiler or linter runs. An
# explicit environment override always wins.
# shellcheck shell=bash
: "${TSC_VERSION:=5.9.3}"
export TSC_VERSION

# WebMod lint stack (scripts/lint-webui.sh). Pinned as versions, not a
# lockfile: the repo deliberately tracks no package.json/node_modules.
: "${OXLINT_VERSION:=1.79.0}"
: "${OXLINT_STANDARDS_VERSION:=0.8.1}"
: "${OXLINT_TSGOLINT_VERSION:=7.0.2001}"
: "${OXLINT_PLUGINS_VERSION:=1.79.0}"
export OXLINT_VERSION OXLINT_STANDARDS_VERSION OXLINT_TSGOLINT_VERSION OXLINT_PLUGINS_VERSION

# Vendored anti-slop plugin source, fetched from a commit tarball outside any
# registry, so the tarball carries no publisher integrity metadata. The SHA
# pins the commit and the SHA256 verifies the downloaded bytes.
# Update both together after inspecting the new upstream source.
: "${ANTI_SLOP_SHA:=6d538555cb151d4121ed51a27db81890eacf8ae9}"
: "${ANTI_SLOP_SHA256:=a720663fd2562e22e3da670769faa88dc34c9a761fdd9a7d285e20d92871848e}"
export ANTI_SLOP_SHA ANTI_SLOP_SHA256

# Nu HTML Checker (vnu-jar) used by scripts/lint-html.sh.
: "${VNU_VERSION:=26.8.20}"
export VNU_VERSION

# The linter used by `make lint-shell`. It has no pinned-distribution step the
# way the bunx tools do (CI installs the distro package), so this records the
# version the gate is written against. scripts/lib/check_shellcheck_version.sh
# prints the installed version and warns on a mismatch: a distro bump can add
# or drop findings with no commit involved, and that must be visible in the log
# rather than silently change the verdict.
# Override: SHELLCHECK_VERSION=0.11.0
: "${SHELLCHECK_VERSION:=0.11.0}"
export SHELLCHECK_VERSION
