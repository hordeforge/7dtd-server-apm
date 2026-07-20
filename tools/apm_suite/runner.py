from __future__ import annotations

import os
import subprocess
import sys
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
