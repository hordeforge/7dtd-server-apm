# Sourced fragment (no shebang by design): one Python interpreter for every
# shell entry point that calls a tools/ script.
# shellcheck shell=bash
# The CLI already runs tools/host_profiler/*.py with the project interpreter
# (backend_python -> sys.executable). Bare `python3` in a shell script would
# silently pick up whatever is first on PATH, which can be older than the
# 3.11 floor in pyproject.toml and fail a capture's post-processing. Resolve
# the same interpreter here: SEVENDTD_APM_PYTHON override, else the project
# venv, else python3.
: "${SEVENDTD_APM_PYTHON:=}"
if [[ -z "$SEVENDTD_APM_PYTHON" ]]; then
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  if [[ -x "$repo_root/.venv/bin/python" ]]; then
    SEVENDTD_APM_PYTHON="$repo_root/.venv/bin/python"
  else
    SEVENDTD_APM_PYTHON="python3"
  fi
fi
command -v "$SEVENDTD_APM_PYTHON" >/dev/null 2>&1 || {
  echo "ERROR: no Python interpreter for the tools/ scripts (set SEVENDTD_APM_PYTHON)" >&2
  exit 1
}
