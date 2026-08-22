from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from .io import atomic_json, file_sha256, load_json
from .models import (
    Artifact,
    BridgeSnapshotV3,
    CollectorResult,
    EventsV2,
    HealthV2,
    ManifestV2,
    MetaV2,
    SummaryV2,
    Target,
    layer_requested,
    schema_dict,
)

REQUIRED = (
    "meta.json",
    "summary.json",
    "health.json",
    "events.json",
    "report.html",
    "dashboard.html",
)
COLLECTORS = {
    "app": ("app_sim", "app/bridge.jsonl"),
    "runtime": ("runtime_gc", "runtime/mono_gc.bt.out"),
    "threads": ("threads", "threads/threads.jsonl"),
    "sync": ("sync_locks", "sync/futex.bt.out"),
    "scheduler": ("scheduler", "scheduler/runqlat.bt.out"),
    "cpu": ("cpu", "cpu/perf/stacks.folded"),
    "memory": ("memory_cache", "memory/proc.jsonl"),
    "io": ("io", "io/vfs.bt.out"),
}
# session-relative JSON documents validated against versioned schemas at ingestion
VALIDATED_DOCUMENTS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("meta.json", MetaV2),
    ("summary.json", SummaryV2),
    ("health.json", HealthV2),
    ("events.json", EventsV2),
    ("app/apm_app.json", BridgeSnapshotV3),
)


def _date(value: Any) -> datetime:
    # Always return an aware UTC datetime: a naive result (from an ISO string
    # without a timezone) would later raise TypeError when compared/subtracted
    # against aware datetimes (e.g. the bridge snapshot stamp).
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _int(value: Any, default: int) -> int:
    # audit_session runs on untrusted/malformed meta.json: a non-numeric value
    # must not crash the audit (its job is to record the problem, not raise).
    # int(float("inf")) raises OverflowError (e.g. JSON "1e999"), not ValueError.
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _validate_documents(session: Path) -> list[str]:
    errors: list[str] = []
    for rel, model in VALIDATED_DOCUMENTS:
        path = session / rel
        if not path.is_file():
            continue
        try:
            model.model_validate(load_json(path))
        except (ValueError, ValidationError) as error:
            first = str(error).splitlines()
            detail = "; ".join(first[:3])
            errors.append(f"schema validation failed for {rel}: {detail}")
    return errors


def _structured_results(session: Path) -> tuple[list[CollectorResult], list[str]]:
    results: list[CollectorResult] = []
    errors: list[str] = []
    for path in sorted(session.glob("*/*.result.json")):
        try:
            results.append(CollectorResult.model_validate(load_json(path)))
        except (ValueError, ValidationError) as error:
            errors.append(f"invalid collector result {path.relative_to(session)}: {error}")
    return results, errors


def _legacy_results(session: Path) -> list[CollectorResult]:
    """Synthesize collector results for sessions captured before result.json existed."""
    collectors: list[CollectorResult] = []
    for name, (layer, rel) in COLLECTORS.items():
        artifact = session / rel
        failed = [p for p in (session / Path(rel).parent).glob("*.err") if p.stat().st_size]
        status: Literal["ok", "failed", "unavailable"] = (
            "failed"
            if failed and not artifact.is_file()
            else "ok"
            if artifact.is_file() and artifact.stat().st_size
            else "unavailable"
        )
        collectors.append(
            CollectorResult(
                name=name,
                layer=layer,
                status=status,
                artifacts=[rel] if artifact.is_file() else [],
                message="; ".join(p.name for p in failed),
            )
        )
    return collectors


def _requested(name: str, layer: str, requested_set: set[str]) -> bool:
    return bool({name, layer} & requested_set) or layer_requested(layer, requested_set)


def list_sessions(root: Path) -> list[Path]:
    """Session directories under root, newest first (mtime)."""
    return sorted(
        (p for p in root.glob("session_*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def sessions_beyond_budget(
    sessions: list[Path], keep: int, max_bytes: float | None = None
) -> list[Path]:
    """Retention policy: everything past the newest `keep`, plus the oldest kept
    sessions until the total fits `max_bytes` when given.

    One implementation feeds both the CLI `prune` command and post-capture
    auto-prune so the two entry points cannot drift into disagreeing about
    which evidence gets deleted.
    """
    doomed = list(sessions[keep:])
    if max_bytes is None:
        return doomed
    sizes = {p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) for p in sessions}
    total = sum(sizes.values()) - sum(sizes[p] for p in doomed)
    for session in reversed(sessions[:keep]):  # oldest kept first
        if total <= max_bytes:
            break
        doomed.append(session)
        total -= sizes[session]
    return doomed


def audit_session(session: Path) -> tuple[ManifestV2, bool]:
    meta = load_json(session / "meta.json") if (session / "meta.json").is_file() else {}
    errors = [
        f"missing or empty: {rel}"
        for rel in REQUIRED
        if not (session / rel).is_file() or not (session / rel).stat().st_size
    ]
    errors += _validate_documents(session)
    warnings: list[str] = []

    collectors, result_errors = _structured_results(session)
    errors += result_errors
    if not collectors:
        collectors = _legacy_results(session)

    only = meta.get("only")
    requested = (only if isinstance(only, str) and only.strip() else "all").split(",")
    requested_set = {token.strip() for token in requested}
    for collector in collectors:
        wanted = _requested(collector.name, collector.layer, requested_set)
        if collector.name == "app" and meta.get("no_app"):
            wanted = False
        if not wanted and collector.status == "unavailable":
            collector.status = "skipped"
        if wanted and collector.status in ("skipped", "unavailable", "failed", "interrupted"):
            warnings.append(
                f"requested collector produced no usable evidence: "
                f"{collector.name} ({collector.status})"
            )

    warning_file = session / "WARN.txt"
    if warning_file.is_file() and warning_file.stat().st_size:
        warnings.append("capture warnings present: WARN.txt")
    events_path = session / "events.json"
    if events_path.is_file():
        with suppress(ValueError, OSError):
            spikes = int((load_json(events_path).get("by_kind") or {}).get("frame_spike") or 0)
            if spikes:
                warnings.append(f"{spikes} frame spikes >= bridge threshold during capture")
    artifacts = [
        Artifact(
            path=p.relative_to(session).as_posix(), bytes=p.stat().st_size, sha256=file_sha256(p)
        )
        for p in sorted(session.rglob("*"))
        # Skip symlinks: a crafted link would otherwise pull a file outside the
        # session into the integrity manifest (and a dir-symlink cycle would hang
        # the walk). Session artifacts are always real files.
        if p.is_file() and not p.is_symlink() and p.name != "manifest.json"
    ]
    manifest = ManifestV2(
        session_id=session.name,
        started_at=_date(meta.get("utc")),
        ended_at=datetime.now(UTC),
        target=Target(
            pid=_int(meta.get("pid"), 1),
            comm=str(meta.get("comm") or "7DaysToDieServe"),
            exe=str(meta.get("exe") or ""),
            cmdline=str(meta.get("cmdline") or ""),
        ),
        requested_layers=requested,
        collectors=collectors,
        artifacts=artifacts,
        warnings=warnings,
        errors=errors,
    )
    atomic_json(session / "manifest.json", schema_dict(manifest))
    return manifest, not errors
