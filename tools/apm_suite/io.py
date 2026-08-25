from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any


def member_is_safe(name: str) -> bool:
    """True when a recorded/archive member path stays inside its base directory.

    Shared guard for every untrusted relative path this tool joins onto a
    session directory (zip members on import, artifact paths recorded in a
    manifest.json that an imported bundle may have planted): an absolute path
    or any ".." segment would otherwise escape the target.
    """
    candidate = PurePosixPath(name)
    return not candidate.is_absolute() and ".." not in candidate.parts


def sync_parent_directory(path: Path) -> None:
    """Fsync the directory so the just-completed rename survives power loss.

    Public because the audit store fsyncs its own renames (trashed sessions);
    the file fsync alone is not enough: without a directory fsync a host crash
    can revert evidence files to empty or missing even though the write
    reported success, which would then fail every later integrity audit.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path.parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
        sync_parent_directory(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Stream a collector JSONL file record by record.

    Shared by every jsonl reader: files can reach tens of MB (each app record
    carries a whole telnet reply), so they are streamed, never held resident.
    Blank and torn lines (a collector killed mid-window by the grace deadline
    or Ctrl+C leaves a truncated final line) are dropped, not fatal; non-object
    records are dropped for the same reason. Yields only dicts.
    """
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def load_json(path: Path) -> dict[str, Any]:
    # Decode failures name the file: a bare "Expecting value" leaves the
    # operator guessing which session artifact was malformed. ValueError (not
    # JSONDecodeError) so every suppress(ValueError)/except ValueError caller
    # keeps catching both failure modes.
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"cannot parse {path}: {error}") from None
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def _next_candidate(base: Path, suffix: int) -> tuple[Path, int]:
    return base.with_name(f"{base.name}_{suffix}"), suffix + 1


def claim_dir(base: Path) -> Path:
    """Create base, or the first free base_1, base_2, ..., and return it.

    Second-resolution timestamps are not unique identity: two captures started
    in the same second must not share one session directory and interleave
    their evidence. A probe-then-create loop cannot guarantee that - both runs
    can observe "free" between the existence check and their mkdir - so the
    claim IS the creation: mkdir fails under a concurrent taker and only that
    loser advances to the next suffix.
    """
    candidate = base
    suffix = 1
    while True:
        try:
            candidate.mkdir(parents=True)
            return candidate
        except FileExistsError:
            candidate, suffix = _next_candidate(base, suffix)


def claim_file(base: Path) -> Path:
    """Exclusive-create twin of claim_dir for file paths (manifests, markers).

    The returned path exists as an empty file the moment this returns, so a
    duplicate run of the same second is assigned a different name instead of
    silently sharing one output path.
    """
    base.parent.mkdir(parents=True, exist_ok=True)
    candidate = base
    suffix = 1
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            candidate, suffix = _next_candidate(base, suffix)
        else:
            os.close(fd)
            return candidate


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
