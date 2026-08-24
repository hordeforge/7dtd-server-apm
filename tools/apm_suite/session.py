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

from .collectors import SPEC_BY_NAME
from .io import atomic_json, file_sha256, load_json, member_is_safe, sync_parent_directory
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
    # `*/**` (not bare `*`): the perf collector's result lands three levels
    # deep (cpu/perf/perf.result.json), and a missed result row would silently
    # drop perf from capture planning/audit instead of flagging its failures.
    for path in sorted(session.glob("*/**/*.result.json")):
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
    """Same --only resolution the capture plan used (models.collector_requested
    over the shared collector catalog), so a collector the plan deliberately
    skipped is never flagged here as "requested but produced no evidence".
    Names missing from the catalog (legacy sessions) fall back to plain
    name/layer/alias matching."""
    spec = SPEC_BY_NAME.get(name)
    if spec is None:
        return bool({name, layer} & requested_set) or layer_requested(layer, requested_set)
    return spec.requested(requested_set)


def _mtime(path: Path) -> float:
    """Sort key tolerant of concurrent prune: a session removed by another
    process between glob and stat would otherwise crash every caller
    (auto-prune after a finished capture, CLI prune) with FileNotFoundError."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def list_sessions(root: Path) -> list[Path]:
    """Session directories under root, newest first (mtime).

    Name breaks mtime ties so ordering (and with it which sessions `prune`
    dooms) never depends on readdir order.
    """
    return sorted(
        (p for p in root.glob("session_*") if p.is_dir()),
        key=lambda p: (_mtime(p), p.name),
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

    # A file deleted by a concurrent prune mid-walk must not crash the budget
    # scan; the vanished bytes count as 0 (the session is going away anyway).
    def _size(path: Path) -> int:
        total = 0
        for f in path.rglob("*"):
            if not f.is_file():
                continue
            try:
                total += f.stat().st_size
            except OSError:
                continue
        return total

    sizes = {p: _size(p) for p in sessions}
    total = sum(sizes.values()) - sum(sizes[p] for p in doomed)
    for session in reversed(sessions[:keep]):  # oldest kept first
        if total <= max_bytes:
            break
        doomed.append(session)
        total -= sizes[session]
    return doomed


TRASH_DIRNAME = ".trash"
# Per-run loadgen manifests/stats written by `scenario run`; each experiment
# leaves two small files behind that nothing else ever deletes.
SCENARIO_DIRNAME = ".scenario"


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


def _move_into_trash(trash: Path, session: Path) -> OSError | None:
    """Rename a session into the trash under a free name.

    The free-name probe and os.replace are two steps, so two concurrent prunes
    can both observe "session_a free" and race the same target; rename(2)
    refuses the loser (ENOTEMPTY against the winner's non-empty directory).
    Detect that collision (target appeared since the probe) and take the next
    suffix instead of reporting a spurious prune failure and leaving the
    session unpruned forever. Bounded so an unrelated OSError (perms, EBUSY)
    still surfaces. A vanished source means a concurrent prune won outright:
    reported as success, matching the store-race contract of the callers.
    """
    for _ in range(16):
        candidate = _free_trash_target(trash, session.name)
        try:
            os.replace(session, candidate)
        except FileNotFoundError:
            # A concurrent prune got there first: the intended end state
            # (session gone from the store) already holds.
            return None
        except OSError as error:
            if candidate.exists():
                continue  # lost that name to a racing prune; take the next
            return error
        # Stamp the grace clock at trash time: the directory mtime is the
        # capture date, which would expire long-lived evidence the moment
        # it is trashed.
        os.utime(candidate, None)
        # The rename itself must survive power loss like every write.
        sync_parent_directory(candidate)
        return None
    return OSError(f"trash name collision persisted for {session.name}")


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
            except FileNotFoundError:
                yield session, None  # a concurrent prune already removed it
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
        yield session, _move_into_trash(trash, session)


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
        except FileNotFoundError:
            # A concurrent purge got there first: same contract as
            # remove_sessions, the intended end state already holds.
            yield entry, None
        except OSError as error:
            yield entry, error
        else:
            yield entry, None


def _scenario_dir(store: Path) -> Path:
    return store / SCENARIO_DIRNAME


def purge_stale_scenario_runs(
    store: Path, grace_hours: float | None = None
) -> Iterator[tuple[Path, OSError | None]]:
    """Delete loadgen manifests and stats under `<store>/.scenario` whose grace
    window has elapsed.

    `scenario run` claims a fresh manifest per invocation (and its stats twin
    lands beside it), so periodic captures on a 24/7 host accumulate files
    forever: unlike session_* directories nothing pruned this run directory.
    The same soft-delete clock as the trash applies (APM_PRUNE_GRACE_HOURS);
    with grace 0 every stale file drops immediately. Only the `loadgen_*`
    family is touched; anything else an operator placed there stays.
    """
    grace = prune_grace_hours() if grace_hours is None else grace_hours
    cutoff = time.time() - max(0.0, grace) * 3600
    scenario = _scenario_dir(store)
    if not scenario.is_dir():
        return
    for entry in sorted(scenario.glob("loadgen_*")):
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            entry.unlink()
        except FileNotFoundError:
            # A concurrent purge got there first: the file is gone, which is
            # the intended end state, not a purge failure.
            yield entry, None
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
        # Recorded paths are untrusted (imported bundles carry their own
        # manifest.json): an absolute path or ".." segment would point this
        # hash check at arbitrary host files outside the session.
        if not member_is_safe(artifact.path):
            errors.append(f"integrity: {artifact.path} escapes the session directory")
            continue
        current = session / artifact.path
        if not current.is_file():
            errors.append(f"integrity: {artifact.path} is recorded but missing")
            continue
        try:
            size = current.stat().st_size
            if size != artifact.bytes:
                errors.append(
                    f"integrity: {artifact.path} changed since the manifest was recorded "
                    f"({artifact.bytes} -> {size} bytes)"
                )
            elif file_sha256(current) != artifact.sha256:
                errors.append(f"integrity: {artifact.path} differs from its recorded hash")
        except OSError as error:
            # An unreadable artifact (perms, vanished mid-audit under a
            # concurrent prune) is itself an integrity finding; crashing here
            # would lose the report for every other recorded artifact too.
            errors.append(f"integrity: {artifact.path} unreadable ({error})")
    return errors


def audit_session(session: Path, *, verify_recorded: bool = False) -> tuple[ManifestV2, bool]:
    # Same untrusted-input contract as _int below: torn/hand-edited JSON must
    # degrade to "no metadata" (and a schema-validation error from
    # _validate_documents), never crash the audit whose job is to record it.
    meta: dict[str, Any] = {}
    if (session / "meta.json").is_file():
        with suppress(ValueError):
            loaded = load_json(session / "meta.json")
            if isinstance(loaded, dict):
                meta = loaded
    errors: list[str] = []
    for rel in REQUIRED:
        try:
            missing_or_empty = not (session / rel).is_file() or not (session / rel).stat().st_size
        except OSError:
            # Vanished between is_file() and stat() (concurrent prune): same
            # contract as the artifacts walk below - count it, don't raise.
            missing_or_empty = True
        if missing_or_empty:
            errors.append(f"missing or empty: {rel}")
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
    artifacts = []
    for p in sorted(session.rglob("*")):
        # Skip symlinks: a crafted link would otherwise pull a file outside the
        # session into the integrity manifest (and a dir-symlink cycle would hang
        # the walk). Session artifacts are always real files.
        if not p.is_file() or p.is_symlink() or p.name == "manifest.json":
            continue
        try:
            artifacts.append(
                Artifact(
                    path=p.relative_to(session).as_posix(),
                    bytes=p.stat().st_size,
                    sha256=file_sha256(p),
                )
            )
        except OSError:
            # A concurrent prune removed the file between glob and read: skip it
            # (same contract as _mtime) instead of crashing every audit that
            # overlaps a prune.
            continue
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
