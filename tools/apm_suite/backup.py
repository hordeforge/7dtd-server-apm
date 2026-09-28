"""Copy the session store somewhere else, and prove the copy reads back.

The store is the only durable state this tool owns and it lives on one host
disk. Every write in the suite is already crash-safe (temp file, fsync,
rename, directory fsync), so instance loss is the disaster left to answer, and
the answer has to be an executed copy plus a read-back check: a backup nobody
has read is a hypothesis. This module is that copy, so the schedule (cron, a
systemd timer, an operator's own rsync) is the only part left outside the tool.

Three properties matter more than throughput here:

* **Owner-only.** Sessions hold raw host evidence, including the telnet
  artifact and any server-log slice the operator dropped in, and the
  destination is expected to be another host. A destination this run creates is
  0700; one that already exists is left at the mode its owner gave it.
* **Atomic per session.** A session is copied into a staging directory and
  renamed into place, so an interrupted run leaves the destination with whole
  sessions or none, never a half-written one that reads as evidence.
* **Verified on every run.** The destination is audited against the recorded
  manifest hashes of everything in it, not only what this run copied, so a
  copy that rotted on the far side fails the backup that found it.
* **Unaffected by retention.** Sessions pruned from the source are not removed
  from the destination: the backup's own record is merged forward, so a store
  trimmed to `--keep 20` does not silently trim the archive to 20 sessions.

The destination is the operator's choice and is expected to be another host or
another filesystem. A destination on the same device as the store is reported
(`same_device`), never refused: it is a legitimate staging step before an
upload, and the tool cannot see past its own filesystem boundary.
"""

from __future__ import annotations

import os
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .io import atomic_json, file_sha256, load_json, sync_parent_directory
from .session import MISSING_PREFIX, SCENARIO_DIRNAME, list_sessions, verify_session

BACKUP_INDEX = "backup-index.json"
BACKUP_SCHEMA = "7dtd.apm.backup.v1"

# Store entries copied beside the sessions: the loadgen manifests and stats a
# scenario run leaves under .scenario, and the index a restored store is
# browsed through. The soft-delete trash is deliberately not copied: its
# contents are already-retired evidence, recoverable with mv on the live host
# for the grace window, and duplicating it would double the destination's size
# for sessions no retention run will ever look at again.
BACKUP_EXTRAS: tuple[str, ...] = (SCENARIO_DIRNAME, "index.html", "index.json")


class BackupError(Exception):
    """The store cannot be copied to the requested destination."""


@dataclass
class BackupReport:
    """What one run did, in the order the operator reads it."""

    destination: Path
    copied: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    invalid: dict[str, list[str]] = field(default_factory=dict)
    incomplete: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    same_device: bool = False

    @property
    def backed_up(self) -> int:
        return len(self.copied) + len(self.unchanged)


def _record_path(destination: Path) -> Path:
    return destination / BACKUP_INDEX


def _read_record(destination: Path) -> dict[str, Any]:
    """The destination's own record of what it holds, or an empty one.

    A truncated or hand-edited record is not a backup failure: the copy is
    re-made from the store either way, and a record that cannot be parsed only
    costs the incremental skip.
    """
    try:
        value = load_json(_record_path(destination))
    except (ValueError, OSError):
        return {}
    sessions = value.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _replace_tree(source: Path, destination: Path) -> None:
    """Copy `source` (a session directory or a store file) through a staging rename.

    rename(2) over a non-empty directory is refused, so the previous copy is
    removed first. The window between the two calls is why the source stays
    authoritative: losing it there costs a rerun, not evidence.
    """
    staging = destination.parent / f".incoming-{os.getpid()}-{source.name}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if source.is_dir():
            shutil.copytree(source, staging)
            shutil.rmtree(destination, ignore_errors=True)
        else:
            shutil.copy2(source, staging)
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # The rename is the entry that makes the copy exist; without the directory
    # fsync a host crash can leave the destination with the bytes but no
    # directory entry, which reads as a session that was never backed up.
    sync_parent_directory(destination)


def _copy_extras(store: Path, destination: Path) -> list[str]:
    copied: list[str] = []
    for name in BACKUP_EXTRAS:
        source = store / name
        if not source.exists():
            continue
        _replace_tree(source, destination / name)
        copied.append(name)
    return copied


def backup_store(store: Path, destination: Path) -> BackupReport:
    """Copy every finalized session in `store` to `destination` and verify it.

    Sessions still capturing (no manifest.json yet) are skipped, not copied:
    their hashes were never baselined, so a copy of them would be evidence with
    nothing to check it against, and the next run picks them up finalized.
    """
    if not store.is_dir():
        raise BackupError(f"not a store directory: {store}")
    resolved_store = store.resolve()
    resolved_destination = destination.absolute()
    if resolved_destination == resolved_store or resolved_destination.is_relative_to(
        resolved_store
    ):
        raise BackupError("destination is inside the store it would back up")
    if resolved_store.is_relative_to(resolved_destination):
        raise BackupError("store is inside the destination; the copy would swallow it")

    # A session carries raw host evidence, the telnet artifact with
    # player-identifying lines, and any server-log slice the operator dropped
    # in, and the destination is expected to be another host or filesystem.
    # A directory this run creates is owner-only from the start, like the store
    # and a restored bundle; an operator-chosen directory that already exists is
    # left at whatever mode they gave it, because this tool does not own it.
    created = not destination.exists()
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BackupError(f"cannot create {destination}: {error}") from error
    if created:
        with suppress(OSError):
            destination.chmod(0o700)

    report = BackupReport(
        destination=destination,
        same_device=os.stat(destination).st_dev == os.stat(store).st_dev,
    )
    recorded = _read_record(destination)
    copied_sessions: dict[str, Any] = {}
    for session in list_sessions(store):
        manifest = session / "manifest.json"
        if not manifest.is_file():
            report.skipped.append(session.name)
            continue
        try:
            fingerprint = file_sha256(manifest)
        except OSError:
            # A manifest that cannot be read (perms, a finalize mid-rename) is
            # reported as skipped, never as backed up.
            report.skipped.append(session.name)
            continue
        if recorded.get(session.name) == fingerprint and (destination / session.name).is_dir():
            report.unchanged.append(session.name)
        else:
            _replace_tree(session, destination / session.name)
            report.copied.append(session.name)
        copied_sessions[session.name] = fingerprint

    report.extras = _copy_extras(store, destination)
    # Merge, never replace: a session pruned from the source keeps its entry,
    # so retention on the live store cannot shrink the archive.
    merged: dict[str, Any] = {
        name: fingerprint for name, fingerprint in recorded.items() if isinstance(fingerprint, str)
    }
    merged.update(copied_sessions)
    atomic_json(
        _record_path(destination),
        {
            "schema": BACKUP_SCHEMA,
            "created": datetime.now(UTC).isoformat(),
            "store": str(resolved_store),
            "sessions": merged,
        },
    )

    # Verify the whole destination, not only this run's copy: a session that
    # rotted here between runs is exactly the drift a backup cannot see.
    for session in list_sessions(destination):
        errors = verify_session(session)
        if not (session / "manifest.json").is_file():
            errors.append("no manifest.json recorded; artifact hashes unverified")
        if errors:
            unverified = all(
                error.startswith((MISSING_PREFIX, "no manifest.json")) for error in errors
            )
            if unverified:
                report.incomplete.append(session.name)
            else:
                report.invalid[session.name] = errors
    return report


def backup_status(destination: Path | None) -> dict[str, Any]:
    """What the last backup of the configured destination looks like.

    The store's copies are only as good as the last run, and a job that stopped
    days ago looks exactly like a healthy store from the outside. `doctor`
    reports this, which is what a monitor alerts on; the age is left to the
    caller's threshold because only the operator knows the schedule.
    """
    if destination is None:
        return {
            "ok": False,
            "destination": None,
            "fix": (
                "no backup destination configured; set SEVENDTD_APM_BACKUP_DIR and run "
                "`7dtd-server-apm backup DEST` on a schedule (the store has no other copy)"
            ),
        }
    record = _record_path(destination)
    try:
        age = time.time() - record.stat().st_mtime
    except OSError:
        return {
            "ok": False,
            "destination": str(destination),
            "fix": f"no backup recorded at {destination}; run `7dtd-server-apm backup {destination}`",
        }
    sessions = _read_record(destination)
    if not sessions:
        return {
            "ok": False,
            "destination": str(destination),
            "age_seconds": age,
            "fix": f"backup record at {destination} lists no session; the last run copied nothing",
        }
    return {
        "ok": True,
        "destination": str(destination),
        "age_seconds": age,
        "sessions": len(sessions),
        "fix": None,
    }
