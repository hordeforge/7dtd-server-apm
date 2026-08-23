from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from .io import _sync_parent_directory, atomic_json, file_sha256, load_json
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
        failed = sorted(p for p in (session / Path(rel).parent).glob("*.err") if p.stat().st_size)
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
    """Session directories under root, newest first (mtime).

    Name breaks mtime ties so ordering (and with it which sessions `prune`
    dooms) never depends on readdir order.
    """
    return sorted(
        (p for p in root.glob("session_*") if p.is_dir()),
        key=lambda p: (p.stat().st_mtime, p.name),
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


TRASH_DIRNAME = ".trash"


def prune_grace_hours() -> float:
    """Soft-delete window for pruned sessions, in hours.

    Pruning is the one mass-destruction path in this tool (a wrong --keep or a
    runaway auto-prune deletes evidence irreversibly), so deletions land in the
    store's trash first and only expire from there. APM_PRUNE_GRACE_HOURS
    overrides; 0 restores immediate hard deletes for space-constrained hosts.
    A non-numeric value warns and falls back to 24h instead of silently
    pretending the operator's setting was read.
    """
    raw = os.environ.get("APM_PRUNE_GRACE_HOURS", "")
    if not raw.strip():
        return 24.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        print(
            f"WARNING: APM_PRUNE_GRACE_HOURS={raw!r} is not a number; using 24",
            file=sys.stderr,
        )
        return 24.0


def keep_sessions_budget() -> int:
    """Retention budget for post-capture auto-prune: keep the newest N sessions.

    APM_KEEP_SESSIONS overrides; <= 0 disables auto-prune. A non-numeric value
    warns and falls back to 40 so a typo cannot silently disable or explode
    retention. Single implementation for capture-time auto-prune and doctor.
    """
    raw = os.environ.get("APM_KEEP_SESSIONS", "")
    if not raw.strip():
        return 40
    try:
        return int(raw)
    except ValueError:
        print(
            f"WARNING: APM_KEEP_SESSIONS={raw!r} is not an integer; using 40",
            file=sys.stderr,
        )
        return 40


def _trash_dir(store: Path) -> Path:
    return store / TRASH_DIRNAME


def _free_trash_target(trash: Path, name: str) -> Path:
    candidate = trash / name
    suffix = 1
    while candidate.exists():
        candidate = trash / f"{name}_{suffix}"
        suffix += 1
    return candidate


def remove_sessions(
    doomed: list[Path], grace_hours: float | None = None
) -> Iterator[tuple[Path, OSError | None]]:
    """Retire sessions past retention via the store's trash directory.

    One implementation for the CLI `prune` command and post-capture auto-prune
    so their failure behavior cannot drift. Sessions are renamed into
    `<store>/.trash/` (same filesystem) and only purge_expired_trash unlinks
    them once the grace window passes, so a bad retention value stays
    recoverable with a plain `mv`. Yields each session with None on success or
    the OSError on failure: a single undeletable session (e.g. EBUSY from a
    leaked mono bind mount) must not abort the whole prune and strand the
    remaining deletions. grace_hours == 0 hard-deletes immediately.
    """
    if not doomed:
        return
    grace = prune_grace_hours() if grace_hours is None else grace_hours
    if grace <= 0:
        for session in doomed:
            try:
                shutil.rmtree(session)
            except OSError as error:
                yield session, error
            else:
                yield session, None
        return
    trash = _trash_dir(doomed[0].parent)
    try:
        trash.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        yield doomed[0], error
        return
    for session in doomed:
        try:
            target = _free_trash_target(trash, session.name)
            os.replace(session, target)
            # Stamp the grace clock at trash time: the directory mtime is the
            # capture date, which would expire long-lived evidence the moment
            # it is trashed.
            os.utime(target, None)
            # The rename itself must survive power loss like every write.
            _sync_parent_directory(target)
        except OSError as error:
            yield session, error
        else:
            yield session, None


def purge_expired_trash(
    store: Path, grace_hours: float | None = None
) -> Iterator[tuple[Path, OSError | None]]:
    """Unlink trashed sessions whose grace window has elapsed.

    Yields each removed entry (or the failure); callers decide how to report.
    With grace disabled (0) any legacy trash is dropped outright so the
    setting cannot leak unbounded disk use.
    """
    grace = prune_grace_hours() if grace_hours is None else grace_hours
    cutoff = time.time() - max(0.0, grace) * 3600
    trash = _trash_dir(store)
    if not trash.is_dir():
        return
    for entry in sorted(trash.glob("session_*")):
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry)
        except OSError as error:
            yield entry, error
        else:
            yield entry, None


def verify_recorded_hashes(session: Path) -> list[str]:
    """Tamper check against the recorded manifest (the `audit` CLI contract).

    Returns one error per recorded artifact whose file is now missing or whose
    size/content no longer matches manifest.json, plus an error when the
    manifest itself is unreadable (the integrity baseline is gone). Empty when
    the session carries no recorded manifest yet (first audit records it).
    """
    path = session / "manifest.json"
    if not path.is_file():
        return []
    try:
        recorded = ManifestV2.model_validate(load_json(path))
    except (ValueError, ValidationError):
        return ["recorded manifest.json is unreadable; integrity baseline lost"]
    errors: list[str] = []
    for artifact in recorded.artifacts:
        current = session / artifact.path
        if not current.is_file():
            errors.append(f"integrity: {artifact.path} is recorded but missing")
            continue
        size = current.stat().st_size
        if size != artifact.bytes:
            errors.append(
                f"integrity: {artifact.path} changed since the manifest was recorded "
                f"({artifact.bytes} -> {size} bytes)"
            )
        elif file_sha256(current) != artifact.sha256:
            errors.append(f"integrity: {artifact.path} differs from its recorded hash")
    return errors


def audit_session(session: Path, *, verify_recorded: bool = False) -> tuple[ManifestV2, bool]:
    meta = load_json(session / "meta.json") if (session / "meta.json").is_file() else {}
    errors = [
        f"missing or empty: {rel}"
        for rel in REQUIRED
        if not (session / rel).is_file() or not (session / rel).stat().st_size
    ]
    # Read the baseline before anything below rewrites manifest.json.
    tampered = verify_recorded_hashes(session) if verify_recorded else []
    errors += tampered
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
    # A failed verification must not rewrite manifest.json: re-stamping the
    # current contents would destroy the recorded baseline the operator needs
    # to see which artifact drifted. Newly attached files (docs: "re-audit a
    # session after deliberately attaching any additional artifact") verify
    # clean and are simply folded into the refreshed manifest.
    if not tampered:
        atomic_json(session / "manifest.json", schema_dict(manifest))
    return manifest, not errors
