"""Support-bundle mechanics: sanitize a session into a shareable zip, restore
one back into the store.

The bundle format is a transport concern with its own rules (what may leave the
host, what may land in the store, how much an archive may claim to expand to),
so it lives here rather than in the CLI: cli.py decides how a failure is shown
and which exit code it maps to, this module decides what a bundle contains and
whether one is safe to extract. Every rejection raises BundleError with a
message meant for the operator.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
import zlib
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .io import claim_dir, json_loads, load_json, member_is_safe, read_text
from .models import SERVER_COMM, Artifact, ManifestV2, Target, as_number, schema_dict
from .session import audit_session, parse_stamp

# Decompression-bomb guard for imported evidence bundles (they arrive from
# other people): CPython's extractall caps each member at its declared
# file_size, so the sum of declared sizes is a reliable upper bound on what
# lands on disk. Sessions are tens of MB; 2 GiB total / 20k members is far
# above any legitimate bundle while stopping a small archive from filling
# the session store volume.
MAX_IMPORT_MEMBERS = 20_000
MAX_IMPORT_UNCOMPRESSED_BYTES = 2 * 1024**3

# Members that must not leave the host whatever they are named. Matched
# case-insensitively on the file name, not on an exact set: the server log is
# PII by content (player names, connect IPs), and an operator attaching a
# slice of it under any name must be excluded the same way bridge.jsonl is,
# not merely home-scrubbed.
# capture bind-mounts the target process's Mono runtime onto this empty
# placeholder so the GC uprobes have a space-free path. What the mount shows is
# the game's own several-MB libmonobdwgc (a third-party binary carrying its own
# license, not evidence this tool produced), and a bundle is written to be
# handed to a stranger, so it stays on the host.
MONO_BIND_MOUNT_NAME = "libmonobdwgc-2.0.so"
EXCLUDED_MEMBER_NAMES = frozenset(
    {"perf.data", "bridge.jsonl", "manifest.json", MONO_BIND_MOUNT_NAME}
)
# Membership is tested against the lowercased member name, so the set is
# lowercased once here: a "Bridge.jsonl" or "PERF.DATA" must be excluded
# exactly like the lowercase spellings the tool writes itself.
_EXCLUDED_LOWERED = {name.lower() for name in EXCLUDED_MEMBER_NAMES}
SERVER_LOG_NAME_MARKERS = ("efficientserver", "output_log")

# Name exclusion is the first layer, not the only one: an operator who drops a
# chat log or a console capture into the session names it whatever they like,
# and every one of those lines is the game's own, carrying player names, connect
# IPs, and Steam IDs. The server stamps every console line with an ISO-8601
# local timestamp ("2026-08-23T10:00:00 4020.512 INF ..."), which no collector
# artifact this tool writes starts a line with, so a streamed text member is
# scrubbed by that shape as well as by its file name. app_scrape.py applies the
# same test on the telnet wire before anything reaches the store; this is the
# same classification one step later, for artifacts this tool did not write.
SERVER_LOG_LINE = re.compile(r"\A\s*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _excluded_member(name: str) -> bool:
    lowered = name.lower()
    return lowered in _EXCLUDED_LOWERED or any(
        marker in lowered for marker in SERVER_LOG_NAME_MARKERS
    )


class BundleError(Exception):
    """A bundle cannot be written, or one offered for import is not safe."""


@dataclass(frozen=True)
class ImportResult:
    session: Path
    valid: bool
    errors: int
    warnings: int
    # The audit findings verbatim, so a caller can name WHICH member drifted
    # instead of printing a count. A restored bundle is the one place the
    # operator learns whether the evidence they just took delivery of is the
    # evidence that was sent.
    findings: tuple[str, ...] = ()


# Keys that carry the raw launch command / binary path. Redacted wherever they
# appear in any JSON doc, at any depth - so summary.json (which embeds the whole
# meta dict) and any future meta-embedding file are covered without a per-filename
# allowlist that silently regresses.
_REDACT_KEYS = {"cmdline", "exe"}


def _scrub(obj: object) -> object:
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if k in _REDACT_KEYS and isinstance(v, str) else _scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _scrub_jsonl_line(line: str, home: str) -> str:
    """Apply the JSON scrub to one line; a malformed line keeps its content
    but still loses the host home prefix."""
    try:
        return json.dumps(_scrub(json.loads(line))).replace(home, "~")
    except json.JSONDecodeError:
        return line.replace(home, "~")


def _stream_scrubbed_member(
    archive: zipfile.ZipFile,
    member: str,
    source: Path,
    home: str,
    *,
    jsonl: bool,
    recorded: list[Artifact],
) -> bool:
    """Stream one text artifact into the archive line by line.

    perf.script and friends reach hundreds of MB in a session; reading one
    resident copy plus its scrubbed twin before writestr spiked RSS by several
    times the artifact size. Line-wise streaming produces the same bytes (every
    kept line is terminated with "\\n", exactly like the former full-text
    splitlines join) while capping memory at one line. Returns False when the
    source could not be opened so the caller can fall back to a raw copy; once
    streaming has started an OSError propagates (the bundle is unusable anyway).

    Unlike str.splitlines this splits only on CR/LF, so exotic separators
    (form feed, NEL, line separator) pass through instead of becoming newlines;
    collector artifacts are line-oriented text and never contain them.

    Server console lines are dropped from text members (SERVER_LOG_LINE): the
    bundle is written to be handed to a stranger, and those lines name players.
    The JSONL path is untouched: a record there is this tool's own structured
    telemetry, already scrubbed field by field.

    `recorded` collects the size and sha256 of the bytes as they are stored, so
    the bundle manifest needs no second decompression pass over the finished
    archive.
    """
    try:
        source_stream = source.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return False

    def scrub(line: str) -> str | None:
        if jsonl:
            return _scrub_jsonl_line(line, home)
        return None if SERVER_LOG_LINE.match(line) else line.replace(home, "~")

    with source_stream, archive.open(member, "w") as member_stream:
        digest = hashlib.sha256()
        size = 0
        for line in source_stream:
            body = scrub(line[:-1] if line.endswith("\n") else line)
            if body is None:
                continue
            payload = body.encode("utf-8") + b"\n"
            digest.update(payload)
            size += len(payload)
            member_stream.write(payload)
    recorded.append(Artifact(path=member, bytes=size, sha256=digest.hexdigest()))
    return True


def _bundle_manifest(session: Path, artifacts: list[Artifact]) -> ManifestV2:
    """Integrity manifest describing the bundle, not the source session.

    The session's own manifest is the descriptive base when it exists; the
    artifact list always comes from the archive. Members the export drops on
    purpose (raw perf.data, telnet bridge.jsonl) must not be recorded, or a
    hand-extracted bundle audits as tampered.
    """
    with suppress(ValueError, OSError, ValidationError):
        recorded = ManifestV2.model_validate(load_json(session / "manifest.json"))
        return recorded.model_copy(
            update={
                "session_id": session.name,
                "ended_at": datetime.now(UTC),
                "artifacts": artifacts,
            }
        )
    meta: dict[str, Any] = {}
    with suppress(ValueError, OSError):
        meta = load_json(session / "meta.json")
    only = str(meta.get("only") or "all")
    # meta.json is as untrusted here as it is in the audit: a hand-edited utc
    # must degrade to a null stamp, not raise a bare ValueError out of the
    # manifest write that closes the export (parse_stamp owns that contract).
    return ManifestV2(
        session_id=session.name,
        started_at=parse_stamp(meta.get("utc")),
        ended_at=datetime.now(UTC),
        target=Target(
            pid=int(as_number(meta.get("pid")) or 1),
            comm=str(meta.get("comm") or SERVER_COMM),
            exe=str(meta.get("exe") or ""),
            cmdline=str(meta.get("cmdline") or ""),
        ),
        requested_layers=only.split(","),
        artifacts=artifacts,
    )


def _stored_bytes(relative: Path, payload: bytes) -> Artifact:
    """Record one member exactly as the archive stored it.

    Every writer in the export walk routes its output through here, so the
    bundle manifest describes the stored bytes (scrubbed, re-serialized) and
    not the source file's.
    """
    return Artifact(
        path=relative.as_posix(), bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()
    )


def _copy_member(
    archive: zipfile.ZipFile, source: Path, relative: Path, recorded: list[Artifact]
) -> None:
    """Raw-copy one artifact into the archive; an unreadable source names the
    file instead of surfacing as a bare traceback mid-export (the .json branch
    in the same walk reports its failures the same way)."""
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as raw, archive.open(str(relative), "w") as member_stream:
            for block in iter(lambda: raw.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
                member_stream.write(block)
    except OSError as error:
        raise BundleError(f"cannot bundle {relative}: {error}") from None
    recorded.append(Artifact(path=relative.as_posix(), bytes=size, sha256=digest.hexdigest()))


def export_bundle(session: Path, output: Path) -> Path:
    """Write a sanitized support bundle of `session` to `output` and return it."""
    if not session.is_dir():
        # Name the path: every sibling command (audit, verify-store, compare)
        # reports a bad session argument with the value the operator typed.
        raise BundleError(f"session directory does not exist: {session}")
    # manifest.json is excluded too: it describes the source session, and the
    # bundle carries its own manifest describing the bundle (below).
    output.parent.mkdir(parents=True, exist_ok=True)
    # An output inside the session would otherwise be swept up by the walk below
    # (a truncated copy of the archive being written, or a prior export of it).
    # Build under a temp path in the destination dir and os.replace on success, so
    # a malformed input never truncates the target or clobbers a prior bundle.
    fd, tmp_zip = tempfile.mkstemp(suffix=".zip", dir=output.parent)
    os.close(fd)
    tmp_zip_path = Path(tmp_zip)
    self_output = {output.resolve(), tmp_zip_path.resolve()}
    try:
        # Scrubbed text members stream straight into the archive: each one is
        # already fully resident for scrubbing, so a temp-dir copy would add a
        # full session-sized write+read for nothing.
        with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            # perf.script / report txt / stacks.folded / flame.html / bpftrace
            # *.out / *.svg embed dso or file paths like /home/<user>/... -
            # replace the home prefix so bundles do not leak the host username.
            # Applied to known-text artifacts only.
            home = str(Path.home())
            text_suffixes = {".txt", ".folded", ".html", ".script", ".md", ".log", ".out", ".svg"}
            # Every writer appends its member's stored size and sha256 here, in
            # archive order, so the manifest below describes the bundle itself.
            # Re-reading the finished zip to hash it meant inflating the whole
            # session a second time on top of the deflate that just stored it.
            recorded: list[Artifact] = []
            # Sorted walk: identical session content must yield an identical
            # member order, not a readdir-order zip layout.
            for source in sorted(session.rglob("*")):
                # Symlinks are skipped like the integrity audit skips them: a
                # planted link (hand-edit, imported-bundle tampering) would
                # otherwise pull an arbitrary file outside the session into a
                # bundle meant for sharing.
                if (
                    not source.is_file()
                    or source.is_symlink()
                    or _excluded_member(source.name)
                    or source.suffix == ".err"
                    or source.resolve() in self_output
                ):
                    continue
                relative = source.relative_to(session)
                if source.suffix == ".json":
                    try:
                        data = json_loads(read_text(source), relative)
                    except (ValueError, OSError) as error:
                        raise BundleError(f"cannot parse {relative}: {error}") from None
                    payload = (json.dumps(_scrub(data), indent=2).replace(home, "~") + "\n").encode(
                        "utf-8"
                    )
                    archive.writestr(str(relative), payload)
                    recorded.append(_stored_bytes(relative, payload))
                elif source.suffix == ".jsonl" or source.suffix in text_suffixes:
                    # Streamed scrub (see _stream_scrubbed_member): the same
                    # per-line transforms as the former read_text+writestr,
                    # without ever holding a full artifact resident.
                    if not _stream_scrubbed_member(
                        archive,
                        str(relative),
                        source,
                        home,
                        jsonl=source.suffix == ".jsonl",
                        recorded=recorded,
                    ):
                        # The stream open failed (raced prune, perms); the raw
                        # copy hits the same wall, so let it name the file.
                        _copy_member(archive, source, relative, recorded)
                else:
                    _copy_member(archive, source, relative, recorded)
        # The integrity manifest travels with the evidence it describes: hash
        # the members as stored and record those, so a hand-extracted bundle
        # audits clean and a tampered member is still detectable.
        with zipfile.ZipFile(tmp_zip_path, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    schema_dict(_bundle_manifest(session, recorded)),
                    indent=2,
                )
                + "\n",
            )
        os.replace(tmp_zip_path, output)
    finally:
        tmp_zip_path.unlink(missing_ok=True)
    return output


def import_bundle(bundle: Path, store: Path) -> ImportResult:
    """Restore an exported support bundle into `store` and audit the result."""
    # NFC at ingestion: a macOS NFD filename and its NFC spelling must claim
    # the same session directory name, or later lookups by the typed form miss.
    stem = "".join(
        c if c.isalnum() or c in "._-" else "_" for c in unicodedata.normalize("NFC", bundle.stem)
    ).strip("._")
    if not stem.startswith("session_"):
        stem = f"session_{stem}"
    try:
        with zipfile.ZipFile(bundle) as archive:
            infos = archive.infolist()
            declared_bytes = sum(info.file_size for info in infos)
            if len(infos) > MAX_IMPORT_MEMBERS or declared_bytes > MAX_IMPORT_UNCOMPRESSED_BYTES:
                raise BundleError(
                    f"refusing bundle beyond import limits ({len(infos)} members, "
                    f"{declared_bytes} uncompressed bytes; "
                    f"max {MAX_IMPORT_MEMBERS}/{MAX_IMPORT_UNCOMPRESSED_BYTES})"
                )
            unsafe = [m for m in archive.namelist() if not member_is_safe(m)]
            if unsafe:
                # Member paths are attacker-controlled (a bundle "from other
                # people"), so they are reported as data, never as markup.
                raise BundleError(
                    f"refusing bundle with unsafe member path(s): {', '.join(unsafe)}"
                )
            # Exclusive-create claim, made only after the bundle is proven safe,
            # so a rejected import never litters the store with an empty session
            # dir; a concurrent duplicate import of the same bundle gets its own
            # target instead of merging into this one mid-extract.
            target = claim_dir(store / stem)
            # Owner-only perms before any member lands (same contract as
            # capture): bundles carry raw evidence, and umask-default perms
            # would leave restored sessions group/world-readable on shared
            # hosts, contradicting docs/APM.md.
            with suppress(OSError):
                store.chmod(0o700)
                target.chmod(0o700)
            try:
                archive.extractall(target)
            except (
                OSError,
                zipfile.BadZipFile,
                zlib.error,
                NotImplementedError,
                RuntimeError,
            ) as error:
                # A mid-extract failure must not strand a partial session that
                # later audits INVALID and pollutes index/prune: remove what
                # landed, then report cleanly. BadZipFile/zlib.error cover the
                # corrupt-member cases (CRC mismatch, broken deflate stream);
                # OSError covers disk-full and unreadable targets;
                # NotImplementedError is a compression method this build has no
                # codec for and RuntimeError is an encrypted member, both
                # chosen by whoever wrote the archive.
                shutil.rmtree(target, ignore_errors=True)
                raise BundleError(
                    f"extraction failed, removed partial import {target}: {error}"
                ) from None
    except (zipfile.BadZipFile, NotImplementedError) as error:
        # NotImplementedError is a zipfile version this build cannot open (a
        # crafted version-needed field), a third way for a stranger's archive
        # to be unreadable and all of them name the bundle the same way.
        raise BundleError(f"{bundle} is not a readable zip bundle: {error}") from None
    # verify_recorded: the bundle carries the manifest that describes its own
    # members, and the plain audit re-stamps manifest.json unconditionally, so
    # an unmodified re-stamp would absorb exactly the drift that manifest
    # exists to find and leave the restored session with no baseline at all.
    # Verifying first keeps a tampered member a reported finding; a clean
    # bundle re-stamps to the local manifest as before.
    manifest, valid = audit_session(target, verify_recorded=True)
    return ImportResult(
        session=target,
        errors=len(manifest.errors),
        warnings=len(manifest.warnings),
        valid=valid,
        findings=tuple(manifest.errors),
    )
