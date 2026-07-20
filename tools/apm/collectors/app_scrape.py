#!/usr/bin/env python3
"""Optional application-layer scrape of the 7dtd-apm bridge via telnet.

Writes app_metrics.jsonl snapshots for APM correlation.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import time
from pathlib import Path


def session(host: str, port: int, password: str, cmds: list[str], timeout: float = 2.0) -> str:
    chunks: list[str] = []
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(timeout)

        def recv() -> str:
            parts: list[bytes] = []
            try:
                while True:
                    d = sock.recv(8192)
                    if not d:
                        break
                    parts.append(d)
                    # Inter-chunk silence window. A laggy server (exactly when we
                    # scrape at scale) can stall mid-response; too short a window
                    # truncates a large reply like `apm capabilities`. The caller's
                    # `timeout` still bounds the total wait.
                    sock.settimeout(0.5)
            except TimeoutError:
                pass
            sock.settimeout(timeout)
            return b"".join(parts).decode("utf-8", errors="replace")

        chunks.append(recv())
        if password:
            sock.sendall((password + "\n").encode())
            chunks.append(recv())
        for c in cmds:
            sock.sendall((c + "\n").encode())
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

    with args.out.open("w") as fh:
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
