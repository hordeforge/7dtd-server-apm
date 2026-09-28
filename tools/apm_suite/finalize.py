"""In-process session finalization pipeline.

Order matters: jitsym annotation → summary → health → events → managed bridge
→ budget → HTML → manifest → index. Each stage is typed and failures are
collected, never swallowed. The manifest stage is the integrity audit, and it
runs after the other stages because re-finalizing rewrites artifacts an earlier
audit recorded.
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analysis.bridge import analyze
from .analysis.budget import check_budget
from .analysis.events import build_events
from .analysis.health import build_health
from .analysis.index import write_index
from .analysis.jitsym import annotate_session
from .analysis.report import build_summary
from .io import json_loads, read_text
from .models import as_mapping, as_number
from .reporting import render_session


@dataclass
class FinalizeResult:
    failed_stages: list[str] = field(default_factory=list)
    # Verdict of the manifest stage (None when that stage did not run or
    # raised). run_capture reads it from here instead of auditing the session
    # a second time: the audit hashes every artifact in the session, and a
    # second pass over a multi-hundred-MB capture after finalize already
    # stamped it bought nothing but duplicate work and a second writer of
    # manifest.json.
    audit_valid: bool | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.failed_stages else 0


def _record_manifest(session: Path) -> bool:
    """(Re)write manifest.json for the finalized session, reporting findings.

    Returns the audit verdict so the caller does not have to re-audit.
    """
    from .session import audit_session

    manifest, valid = audit_session(session)
    for error in manifest.errors:
        print(f"finalize: audit error: {error}", file=sys.stderr)
    for warning in manifest.warnings:
        print(f"finalize: audit warning: {warning}", file=sys.stderr)
    return valid


def finalize(session: Path, skip_bridge: bool = False) -> FinalizeResult:
    result = FinalizeResult()

    def stage(name: str, action: Callable[[], object], *, required: bool) -> None:
        print(f">> finalize: {name}")
        try:
            action()
        except Exception:  # noqa: BLE001 -- a stage failure must not abort the rest
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

    def record_manifest() -> None:
        result.audit_valid = _record_manifest(session)

    # Every session ships an integrity manifest (docs/APM.md "Validity"), and a
    # re-finalize rewrites artifacts a previous audit recorded: re-stamp last so
    # the manifest describes the session as it stands after this run. Required,
    # not optional: a session left with no manifest is one whose hashes were
    # never baselined, and the next audit would record whatever it finds as the
    # baseline, absorbing exactly the drift the manifest exists to catch. The
    # run must say the write failed instead of exiting 0 over a session that
    # cannot be verified.
    stage("manifest", record_manifest, required=True)
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
        meta = as_mapping(_load_object(summary_path).get("metadata"))
        lag = as_mapping(meta.get("lag_diagnosis"))
        if lag.get("verdict"):
            print(f">> lag diagnosis: {lag['verdict']}")
        if lag.get("profile"):
            print(f">> {lag['profile']}")
        hint = _churn_hint(meta)
        if hint:
            print(f">> hint: {hint}")
    return result


def _churn_hint(meta: dict[str, Any]) -> str | None:
    """Hint when GC churn is high but no site is named, else None.

    net heap growth reads ~0 under churn, so the gross rate and the full-GC
    count are the signals; both are coerced, so a hand-edited summary costs
    the hint instead of raising out of a finalize that already succeeded.
    """
    gc_meta = as_mapping(meta.get("gc"))
    gross = as_number(gc_meta.get("grossAllocMBPerSecond"))
    full_gc = as_number(gc_meta.get("fullCollections")) or 0
    high_churn = (gross is not None and gross >= 4) or full_gc >= 1
    if not high_churn or meta.get("top_churn_sites"):
        return None
    return (
        "significant GC churn but the allocating sites are unnamed; re-capture "
        "with --only alloc,app to attribute it (top_churn_sites / top_alloc_sites)"
    )


def _load_object(path: Path) -> dict[str, Any]:
    """Read a session document as an object, or {} when it is unreadable.

    The finalization stages already ran and reported their own failures; a
    broken summary.json costs the operator the console hints, not a traceback
    out of a finalize that otherwise succeeded.
    """
    try:
        return as_mapping(json_loads(read_text(path), path))
    except (ValueError, OSError):
        return {}
