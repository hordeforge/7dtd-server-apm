from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import IO, Any

# Lone surrogates (JSON "\ud800" escapes; a pair decodes to two lone halves)
# cannot be encoded to UTF-8, so any survivor would crash os.stat joins and
# every atomic_* writer downstream. Untrusted readers scrub them here, once,
# instead of each consumer guessing whether its text is encodable.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_SURROGATE_MAP = dict.fromkeys(range(55296, 57344), "�")


def _clean_str(value: str) -> str:
    # translate() only pays on strings that actually contain a surrogate.
    return value.translate(_SURROGATE_MAP) if _SURROGATE_RE.search(value) else value


def _sans_surrogates(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, list):
        return [_sans_surrogates(item) for item in value]
    if isinstance(value, dict):
        return {_clean_str(key): _sans_surrogates(item) for key, item in value.items()}
    return value


def force_utf8_stdio() -> None:
    """Pin stdout and stderr to UTF-8 whatever the process locale says.

    Under LANG=C (a bare systemd unit, cron, `env -i`, `sudo` without
    -E) both streams are ASCII, and the first non-ASCII character a command
    prints raises UnicodeEncodeError instead of being reported: a session
    path under a non-ASCII home directory, a hostname quoted inside an
    OSError, a localized tool version. Rich writes straight to the text
    stream and does not guard, so the traceback lands where the report
    should be. Every file this tool writes is already UTF-8; these two
    streams were the last boundary inheriting the environment.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # Not a TextIOWrapper: a test harness' or a wrapper's stand-in
            # that owns its own encoding. There is nothing to reconfigure.
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except ValueError as error:
            # Already-detached or already-read stream: reconfigure refuses.
            # Its own encoding still applies, and a warning written through
            # the stream that just failed could raise again, so it goes out
            # as bytes through stderr's buffer instead.
            _write_bytes(
                getattr(sys.stderr, "buffer", sys.stderr),
                f"WARNING: cannot pin stdio to UTF-8: {error}\n".encode(),
            )


def _write_bytes(buffer: Any, payload: bytes) -> None:
    sys.stdout.flush()
    buffer.write(payload)
    buffer.flush()


def write_stdout(text: str) -> None:
    """Emit UTF-8 on stdout whatever the process locale says.

    The machine-readable `--json -` output is a document a caller pipes into
    another tool: it must be UTF-8 like every file this tool writes, not
    whatever `LANG` happens to be on the host that produced it.
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    _write_bytes(buffer, text.encode("utf-8", "replace"))


def read_stdin_text(stream: IO[str] | None = None) -> Iterator[str]:
    """Iterate stdin as UTF-8 with undecodable bytes replaced.

    sys.stdin follows the locale too, and its default error handler is
    surrogateescape, so under LANG=C the same bytes read from a pipe take a
    different path than the identical bytes read from a file: they survive as
    lone surrogates that a later UTF-8 writer cannot encode. Reconfiguring
    pins the pipe to the file policy.
    """
    source = sys.stdin if stream is None else stream
    reconfigure = getattr(source, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    return source


def member_is_safe(name: str) -> bool:
    """True when a recorded/archive member path stays inside its base directory.

    Shared guard for every untrusted relative path this tool joins onto a
    session directory (zip members on import, artifact paths recorded in a
    manifest.json that an imported bundle may have planted): an absolute path,
    any ".." segment, or a lone-surrogate spelling (unencodable, so no such
    file can exist on this host yet still crash the join) is rejected.
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
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
        stream = os.fdopen(fd, "w", encoding="utf-8", newline="")
    except BaseException:
        # fdopen is the only step that can fail between mkstemp handing back an
        # open descriptor and the `with` owning it; bail out closing the raw fd
        # so a failure here cannot leak one per atomic write.
        with suppress(OSError):
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    try:
        with stream:
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
    records are dropped for the same reason. Yields only dicts. Lone-surrogate
    escapes in planted records are scrubbed like every other untrusted reader.
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
                yield _sans_surrogates(record)


def scrape_succeeded(path: Path) -> bool:
    """True when an app_scrape log holds at least one successful record.

    The collector logs every attempt, so an unreachable telnet endpoint or a
    rejected password fills the file with ok:false records while the collector
    itself exits 0. Artifact presence alone would then read as collected
    evidence. An unreadable file is not this predicate's call to make: the
    caller decides on size and permissions, so a read failure reports True.
    """
    try:
        return any(record.get("ok") is True for record in iter_jsonl(path))
    except OSError:
        return True


def load_json(path: Path) -> dict[str, Any]:
    # Decode failures name the file: a bare "Expecting value" leaves the
    # operator guessing which session artifact was malformed. ValueError (not
    # JSONDecodeError) so every suppress(ValueError)/except ValueError caller
    # keeps catching both failure modes. The parsed document is scrubbed of
    # lone surrogates: imported bundles plant JSON here, and a survivor would
    # crash the writers and path joins every caller feeds it into.
    try:
        value = _sans_surrogates(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as error:
        raise ValueError(f"cannot parse {path}: {error}") from None
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def strip_json_comments(text: str) -> str:
    """Drop // and /* */ comments from JSON text, outside strings only.

    The bridge's Config/apmbridge.json is hand-edited and ships with
    comments, which the mod's own reader accepts and json.loads does not. A
    character walk (not a regex) is what keeps a "http://" inside a string
    value from eating the rest of the line; comment text is replaced by its
    newlines so a parse error still points at the operator's line.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            out.append(char)
            index += 1
        elif char == "/" and text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
        elif char == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            stop = length if end == -1 else end + 2
            out.append("\n" * text.count("\n", index, stop))
            index = stop
        else:
            out.append(char)
            index += 1
    return "".join(out)


def load_jsonc(path: Path) -> Any:
    """Read a comment-carrying JSON config, naming the file on a decode error.

    Same ValueError contract as load_json, but it returns whatever the
    document holds rather than insisting on an object: the bridge config
    readers must be able to see a valid non-object document and diagnose it.
    """
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(strip_json_comments(text))
    except json.JSONDecodeError as error:
        raise ValueError(f"cannot parse {path}: {error}") from None


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
            # Owner-only from the creating syscall: a chmod after mkdir would
            # leave the directory group/world-readable for as long as the
            # umask-default mode stands, and a session holds raw host evidence.
            candidate.mkdir(parents=True, mode=0o700)
            return candidate
        except FileExistsError:
            candidate, suffix = _next_candidate(base, suffix)


def claim_file(base: Path) -> Path:
    """Exclusive-create twin of claim_dir for file paths (manifests, markers).

    The returned path exists as an empty file the moment this returns, so a
    duplicate run of the same second is assigned a different name instead of
    silently sharing one output path. It is created owner-only: a loadgen
    manifest names the experiment and the store it ran against, and the
    umask-default mode would leave it readable to every local account.
    """
    base.parent.mkdir(parents=True, exist_ok=True)
    candidate = base
    suffix = 1
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
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
