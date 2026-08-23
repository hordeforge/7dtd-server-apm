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


def apm_root() -> Path:
    """Session data root. Sessions are data, not source; keep them out of the repo."""
    return _env_path("SEVENDTD_APM_DIR", Path.home() / ".local/share/7dtd-apm")
