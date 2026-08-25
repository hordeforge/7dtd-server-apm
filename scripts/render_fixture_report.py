#!/usr/bin/env python3
"""Render report.html + dashboard.html for a fixture session directory.

lint-html.sh hands vnu real template output by rendering a minimal fixture
session with the suite's own reporting pipeline. Kept as a proper script file,
not a shell heredoc, so ruff/format/mypy gate it like every other Python
source in this repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Same checkout bootstrap as tools/host_profiler/flame_diff_html.py: resolve
# apm_suite from the repository when it is not installed in this interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from apm_suite import reporting


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(__file__).name} SESSION_DIR", file=sys.stderr)
        return 2
    reporting.render_session(Path(argv[1]))
    print("rendered report + dashboard templates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
