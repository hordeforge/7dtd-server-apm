#!/usr/bin/env python3
"""Optional application-layer scrape of the 7dtd-server-apm bridge via telnet.

Persists only the `apm status` / `apm capabilities` / `apm dump` replies as
JSONL snapshots (the --out target, app/bridge.jsonl in a capture session) for
APM correlation; streamed console-log lines are filtered out.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import time
from pathlib import Path

# The dedicated server streams its console log to telnet clients between
# command replies. Those lines begin with the game's ISO timestamp prefix
# ("2026-08-23T10:00:00 4020.512 INF ...") and carry player names, IPs, and
# Steam IDs; bridge command replies never use that shape. Lines are judged
# only once complete (a reply or a streamed line may arrive split across
# reads, so the unfinished tail is carried into the next chunk and rejoined),
# and the fragment left over when the socket closes is discarded rather than
# persisted: without its head it cannot be classified.
#
# The timestamp is searched, not anchored: nothing guarantees the server ends
# a stream write with a newline, so a streamed log line can land glued to the
# tail of the command reply before it. An anchored match would keep that whole
# line, player data included. Everything from the first timestamp on is the
# server's log, so the reply text ahead of it is what gets kept.
STREAMED_LOG_LINE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def session(host: str, port: int, password: str, cmds: list[str], timeout: float = 2.0) -> str:
    chunks: list[str] = []
    # The reassembly buffer stays bytes: decoding per chunk would turn any
    # multi-byte UTF-8 sequence split across two reads into U+FFFD pairs.
    # Line separators (\n, \r) are pure ASCII so they can never occur inside
    # a multi-byte sequence, making byte-level splitting safe.
    pending = b""

    def feed(raw: bytes) -> str:
        """Filter one received chunk into persistable text: each complete line
        is cut at the first console-log timestamp, keeping only the reply text
        ahead of it; the trailing partial line waits for the rest of itself."""
        nonlocal pending
        stream = pending + raw
        *lines, pending = stream.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
        kept = [
            STREAMED_LOG_LINE.split(line.decode("utf-8", errors="replace"), maxsplit=1)[0]
            for line in lines
        ]
        return "".join(line + "\n" for line in kept if line)

    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(timeout)

        def recv() -> str:
            parts: list[str] = []
            try:
                while True:
                    d = sock.recv(8192)
                    if not d:
                        break
                    parts.append(feed(d))
                    # Inter-chunk silence window. A laggy server (exactly when we
                    # scrape at scale) can stall mid-response; too short a window
                    # truncates a large reply like `apm capabilities`. The caller's
                    # `timeout` still bounds the total wait.
                    sock.settimeout(0.5)
            except TimeoutError:
                pass
            sock.settimeout(timeout)
            return "".join(parts)

        # The greeting and the post-logon reply are drained only to keep the
        # protocol in step; they are discarded so the persisted scrape holds
        # just the requested command responses. Everything the server streams,
        # around logon or between commands (banner text, echoed input, chat,
        # log lines naming players), is server-controlled and must not reach
        # the session store.
        recv()
        if password:
            sock.sendall((password + "\n").encode("utf-8"))
            recv()
        for c in cmds:
            sock.sendall((c + "\n").encode("utf-8"))
            time.sleep(0.1)
            chunks.append(f">>> {c}\n" + recv())
        with contextlib.suppress(OSError):
            sock.sendall(b"exit\n")
    return "\n".join(chunks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--interval", type=float, default=10)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    password = os.environ.get("SEVENDTD_TELNET_PASSWORD", "")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Monotonic deadline so a wall-clock shift can't cut the window short or run
    # it long; the per-record `t` stays wall-clock for cross-log correlation.
    end = time.monotonic() + args.seconds
    cmds = ["apm status", "apm capabilities", "apm dump"]

    # Owner-only, like every other evidence file this suite writes: a capture
    # session is 0700, but this collector also runs standalone against a
    # caller-chosen --out, and the records hold replies read off the telnet
    # wire. chmod after the open so a re-run over an existing file tightens it
    # too; os.open's mode alone would only apply at creation.
    descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
        with contextlib.suppress(OSError):
            os.chmod(args.out, 0o600)
        while time.monotonic() < end:
            t = time.time()
            try:
                text = session(args.host, args.port, password, cmds)
                rec = {"t": t, "ok": True, "text": text}
            except OSError as e:
                rec = {"t": t, "ok": False, "error": str(e)}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print(f"app_scrape ok={rec.get('ok')} t={t:.0f}")
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
