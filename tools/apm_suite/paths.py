from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"
APM_BACKENDS = TOOLS / "apm"
DEFAULT_DS = Path.home() / ".local/share/Steam/steamapps/common/7 Days to Die Dedicated Server"


def _env_path(name: str, default: Path) -> Path:
    """Resolve a directory override; an exported-but-empty value must not
    collapse to Path("") == cwd and point sessions or probes at the repo."""
    value = os.environ.get(name, "")
    return Path(value) if value.strip() else default


def dedicated_dir() -> Path:
    # SEVENDTD_GAME_DIR (client install) is a bridge-build knob consumed by
    # scripts/build_bridge.sh directly; no runtime code needs it.
    return _env_path("SEVENDTD_DS_DIR", DEFAULT_DS)


def bridge_mod_dir() -> Path:
    """Installed bridge mod folder under the dedicated server.

    Single source for every Python reader of Mods/7dtd-server-apm-bridge so
    the mod folder name and DS override cannot drift apart. capture's
    /proc/<pid>/exe-based resolution is deliberately separate: it follows a
    running process, not the configured install.
    """
    return dedicated_dir() / "Mods" / "7dtd-server-apm-bridge"


def apm_root() -> Path:
    """Session data root. Sessions are data, not source; keep them out of the repo."""
    return _env_path("SEVENDTD_APM_DIR", Path.home() / ".local/share/7dtd-server-apm")


def require_backends() -> None:
    """Fail fast when the collector backends beside this package are absent.

    The wheel ships only apm_suite; capture and flamegraph features shell out
    to tools/apm and tools/host_profiler, which exist only in a repository
    checkout. Raise instead of letting every collector fail one by one with
    file-not-found noise.
    """
    if not (APM_BACKENDS / "collectors").is_dir():
        raise RuntimeError(
            f"collector backends missing at {APM_BACKENDS}; this command needs a "
            "repository checkout (`uv sync && uv run 7dtd-server-apm ...`)"
        )
