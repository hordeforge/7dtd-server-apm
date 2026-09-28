#!/usr/bin/env bash
# Report the installed shellcheck against the version `make lint-shell` is
# written against (SHELLCHECK_VERSION in scripts/lib/tool_versions.sh).
#
# This tool has no pinned-distribution step the way the bunx tools do: CI
# installs the distro package, so a runner image bump can add or drop findings
# with no commit involved. That must be visible in the log rather than silently
# change the gate's verdict, so a mismatch is a warning and never a failure.
# New major releases ship new checks, so a mismatch is expected sometimes and
# must not break an unrelated pull request.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/scripts/lib/tool_versions.sh"

installed="$(shellcheck --version | sed -n 's/^version: //p' | head -n1)"
if [[ -z "$installed" ]]; then
  echo "WARNING: could not parse the shellcheck version; gate ran against an unknown build" >&2
  exit 0
fi
if [[ "$installed" != "$SHELLCHECK_VERSION" ]]; then
  echo "WARNING: shellcheck $installed installed, gate written against $SHELLCHECK_VERSION" >&2
  echo "WARNING: a version change can add or drop findings; update SHELLCHECK_VERSION in scripts/lib/tool_versions.sh if the new findings are right" >&2
fi
