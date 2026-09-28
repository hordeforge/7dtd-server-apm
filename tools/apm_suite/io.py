from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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
# A lone surrogate can only ENTER a parsed document through a \uD800-\uDFFF
# escape: every reader here decodes UTF-8 strictly (or replaces undecodable
# bytes), so no raw surrogate code unit survives to be handed to json.loads.
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89abcdefABCDEF][0-9a-fA-F]{2}")
_SURROGATE_MAP = dict.fromkeys(range(55296, 57344), "�")

# Characters that must never appear in an untrusted path this tool will create
# or report: C0 and C1 controls (a newline or CR in a filename is legal on
# Linux and splits the name for every line-oriented reader), the bidi overrides
# and isolates, and the zero-width joiner/space and BOM characters. They render
# as nothing, so a path that differs only by them compares equal to the eye
# while naming a different file.
_INVISIBLE_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200d\u202a-\u202e\u2060\u2066-\u2069\ufeff]"
)


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
    any ".." segment, a lone-surrogate spelling (unencodable, so no such
    file can exist on this host yet still crash the join), or a name carrying
    invisible/bidi controls is rejected. A name that normalizes to no path
    part at all ("" or ".") is rejected with them: it names no member, and
    extraction raises ValueError on it rather than writing anything.

    The control rule covers C0/C1 controls (a newline or CR in a member name
    is legal on Linux and produces a file whose name every line-oriented tool
    downstream, this suite included, reads as two names) and the format
    characters that render as nothing: the bidi overrides and isolates (U+202A
    to U+202E, U+2066 to U+2069) and the zero-width joiners and spaces
    (U+200B to U+200D, U+FEFF, U+2060). Evidence this tool writes is ASCII, so
    no legitimate member is rejected.
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if _INVISIBLE_RE.search(name):
        return False
    candidate = PurePosixPath(name)
    if not candidate.parts:
        # "" and "." normalize to the base directory itself: they name no
        # member, and extracting one raises ValueError("Empty filename").
        return False
    return not candidate.is_absolute() and ".." not in candidate.parts


def regular_file_size(path: Path) -> int | None:
    """Size of a regular file, else None (missing, unreadable, or a directory).

    One stat per path: is_file() followed by stat() is two syscalls for one
    answer, and a racy pair, since a concurrent prune can remove the entry
    between them. Callers compare the returned size against their own threshold
    so the "carries data" rule stays named at the call site.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


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
                record = json_loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


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


# A session document is a handful of levels deep; imported bundles and hand
# edits are not. Past this depth nothing downstream can use the value anyway:
# the recursive scrub, json.dumps on write, and every consumer walk would each
# hit the interpreter recursion limit, so the document is rejected at the
# boundary instead of dying somewhere less legible.
MAX_JSON_DEPTH = 200


def _too_deep(value: Any) -> bool:
    """Iterative depth check; a recursive walk would itself blow the stack on
    the very documents it is meant to reject."""
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            return True
        if isinstance(node, dict):
            pending.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    return False


def read_text(path: Path) -> str:
    """Read an untrusted artifact as UTF-8, naming the file on a decode failure.

    A session artifact written by a foreign tool, a Latin-1 hand edit, or a
    truncated capture is not UTF-8; the raw UnicodeDecodeError names a byte
    offset and nothing else, and it is not an OSError, so readers guarding
    `except (json.JSONDecodeError, OSError)` let it out as a traceback.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"cannot decode {path}: {error}") from None


def json_loads(text: str, source: object | None = None) -> Any:
    """Decode untrusted JSON text, naming the source on every failure.

    Three failures besides a syntax error reach these readers from imported
    bundles: a non-UTF-8 file raises UnicodeDecodeError, a document nested
    thousands deep exhausts the scanner's recursion budget with RecursionError,
    and one that survives the scanner still kills the recursive scrub and every
    writer behind it. Every caller guards `except (json.JSONDecodeError,
    ValueError)`, so the first two escaped as tracebacks out of a stage meant
    to degrade to absent evidence. All three become the ValueError contract.
    """
    where = f" in {source}" if source is not None else ""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"cannot parse{where}: {error}") from None
    except UnicodeDecodeError as error:
        raise ValueError(f"cannot decode{where}: {error}") from None
    except RecursionError:
        raise ValueError(f"cannot parse{where}: JSON nested too deeply") from None
    if _too_deep(value):
        raise ValueError(f"cannot parse{where}: JSON nested deeper than {MAX_JSON_DEPTH}")
    # Scrubbed here, once, for every reader. The pre-scan is worth its line:
    # _sans_surrogates rebuilds every nested list and dict, which costs several
    # times the parse itself on a multi-hundred-KB session document, and a
    # session store is full of them (one summary per retained session for the
    # index, plus the render and audit passes). One C-level scan of the raw text
    # decides whether that walk has anything to find.
    return _sans_surrogates(value) if _SURROGATE_ESCAPE.search(text) else value


def load_json(path: Path) -> dict[str, Any]:
    # Decode failures name the file: a bare "Expecting value" leaves the
    # operator guessing which session artifact was malformed. ValueError (not
    # JSONDecodeError) so every suppress(ValueError)/except ValueError caller
    # keeps catching both failure modes. Lone surrogates are scrubbed inside
    # json_loads: imported bundles plant JSON here, and a survivor would crash
    # the writers and path joins every caller feeds it into.
    value = json_loads(read_text(path), path)
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
    return json_loads(strip_json_comments(read_text(path)), path)


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


def has_bytes(path: Path, minimum: int = 0) -> bool:
    """One stat for "is a regular file carrying at least `minimum` bytes".

    is_file() followed by stat() is two syscalls for one answer and a racy
    pair; a concurrent prune can remove the entry between them. A vanished or
    unreadable path is not evidence, so both answer False.
    """
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > minimum


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
