from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from rich.console import Console

console = Console()


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> int:
    shown = command.copy()
    for flag in ("--telnet-pass", "--telnet-password", "--password"):
        if flag in shown:
            position = shown.index(flag)
            if position + 1 < len(shown):
                shown[position + 1] = "<redacted>"
    console.print("[dim]$ " + " ".join(shown) + "[/dim]")
    process_env = os.environ.copy()
    process_env.update(env or {})
    result = subprocess.run(command, cwd=cwd, check=False, env=process_env)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)
    return result.returncode


def backend_python(script: Path, args: list[str]) -> int:
    return run([sys.executable, str(script), *args], check=False)


def terminate_tree(
    process: subprocess.Popen[bytes], *, term_grace: float = 10, kill_grace: float = 5
) -> int | None:
    """Stop a start_new_session process together with its whole process group.

    Signaling only the direct child orphans grandchildren (a launcher shell's
    bot binary keeps its sockets open long after the shell died), so the group
    gets SIGTERM, a bounded wait, then SIGKILL. Root-owned children that ignore
    signals are bounded the same way as capture._terminate: no wait here can
    hang shutdown. Returns the reaped returncode, or None when it never exited.
    """
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        return process.wait(timeout=term_grace)
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        return process.wait(timeout=kill_grace)
    except subprocess.TimeoutExpired:
        # Reap if it exited right after the SIGKILL so we don't leave a zombie
        # behind (a still-D-state child can't be reaped by anyone).
        process.poll()
        return None
