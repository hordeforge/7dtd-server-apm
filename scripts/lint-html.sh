#!/usr/bin/env bash
# vnu (Nu HTML Checker) over the HTML and CSS this repo ships.
#
# Covers:
#   - the APM capture report templates (rendered via the suite's own
#     reporting.render_session with a minimal fixture session),
#   - the golden report test fixture,
#   - the bridge WebMod styling.css (embedded in a scratch document, checked
#     with --also-check-css; the WebMod itself is JS-rendered, no HTML).
#
# vnu runs through npx pinned by VNU_VERSION (the same convention as zdtd);
# vnu-jar is a Java tool, so java is required. vnu-filter.txt drops deliberate
# deviations; anything else fails the gate. Warnings do not fail.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
vnu_version="${VNU_VERSION:-26.8.20}"

command -v npx >/dev/null 2>&1 || {
  echo "7dtd-apm: lint-html: npx (Node.js/npm) not found; vnu runs through pinned npx packages" >&2
  exit 1
}
command -v java >/dev/null 2>&1 || {
  echo "7dtd-apm: lint-html: java not found; vnu-jar needs a Java runtime (e.g. apt install default-jre)" >&2
  exit 1
}

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# Render the report + dashboard templates with a minimal fixture session.
mkdir -p "$scratch/session"
cat > "$scratch/session/summary.json" <<'JSON'
{"meta": {"session_id": "vnu-lint-fixture", "collected_at": "2026-01-01T00:00:00Z"}, "layers": [], "metadata": {}}
JSON
(
  cd "$root"
  uv run --project "$root" python - "$scratch/session" <<'PY'
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(os.getcwd()) / "tools"))
from apm_suite import reporting
reporting.render_session(Path(sys.argv[1]))
print("rendered report + dashboard templates")
PY
)

# Embed the bridge stylesheet in a scratch document so --also-check-css sees it.
css="$root/bridge/ApmBridge/WebMod/styling.css"
{
  printf '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8"><title>apm-bridge css check</title><style>\n'
  cat "$css"
  printf '\n</style></head><body></body></html>\n'
} > "$scratch/css-check.html"

mapfile -t html_files < <(
  find "$scratch" -name '*.html' | sort
  echo "$root/tools/apm_suite/tests/fixtures/golden_report.html"
)

echo "vnu: checking ${#html_files[@]} HTML documents (+ embedded CSS)"
npx --yes "vnu-jar@$vnu_version" --also-check-css --filterfile "$root/vnu-filter.txt" "${html_files[@]}"
echo "vnu: OK"
