"""In-process session finalization pipeline.

Order matters: jitsym annotation → summary → health → events → managed bridge
→ budget → HTML → index. Each stage is typed and failures are collected, never
swallowed; the integrity audit runs separately, after every session file is
closed.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .analysis.bridge import analyze
from .analysis.budget import check_budget
from .analysis.events import build_events
from .analysis.health import build_health
from .analysis.index import write_index
from .analysis.jitsym import annotate_session
from .analysis.report import build_summary
from .reporting import render_session


@dataclass
class FinalizeResult:
    session: Path
    failed_stages: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed_stages else 0


def finalize(session: Path, skip_bridge: bool = False) -> FinalizeResult:
    result = FinalizeResult(session=session)

    def stage(name: str, action: Callable[[], object], *, required: bool) -> None:
        print(f">> finalize: {name}")
        try:
            action()
        except Exception:
            traceback.print_exc()
            if required:
                result.failed_stages.append(name)

    stage("jitsym", lambda: annotate_session(session), required=False)
    stage("summary", lambda: build_summary(session), required=True)
    stage("health", lambda: build_health(session), required=True)
    stage("events", lambda: build_events(session), required=True)
    if not skip_bridge:
        stage("bridge", lambda: analyze(session), required=False)

    # The gate's pass/fail lives in budget_check.txt/.json and the explicit
    # `budget` command; finalize's exit code tracks failed stages only.
    stage("budget", lambda: check_budget(session), required=False)
    stage("render", lambda: render_session(session), required=True)
    stage("index", lambda: write_index(), required=False)

    if result.failed_stages:
        print(
            "required finalization stages failed: " + ", ".join(result.failed_stages),
            file=sys.stderr,
        )
    else:
        print(f"finalized {session}")
    summary_path = session / "summary.json"
    if summary_path.is_file():
        with suppress(Exception):
            meta = json.loads(summary_path.read_text(encoding="utf-8")).get("metadata") or {}
            lag = meta.get("lag_diagnosis") or {}
            if lag.get("verdict"):
                print(f">> lag diagnosis: {lag['verdict']}")
            if lag.get("profile"):
                print(f">> {lag['profile']}")
            gc_meta = meta.get("gc") or {}
            gross = gc_meta.get("grossAllocMBPerSecond")
            high_churn = (gross is not None and float(gross) >= 4) or int(
                gc_meta.get("fullCollections") or 0
            ) >= 1
            if high_churn and not meta.get("top_churn_sites"):
                print(
                    ">> hint: significant GC churn but the allocating sites are "
                    "unnamed; re-capture with --only alloc,app to attribute it "
                    "(top_churn_sites / top_alloc_sites)"
                )
    return result
