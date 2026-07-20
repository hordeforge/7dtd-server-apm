from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"
APM_BACKENDS = TOOLS / "apm"
DEFAULT_DS = Path.home() / ".local/share/Steam/steamapps/common/7 Days to Die Dedicated Server"
DEFAULT_CLIENT = Path.home() / ".local/share/Steam/steamapps/common/7 Days To Die"


def dedicated_dir() -> Path:
    return Path(os.environ.get("SEVENDTD_DS_DIR", DEFAULT_DS))


def client_dir() -> Path:
    return Path(os.environ.get("SEVENDTD_GAME_DIR", DEFAULT_CLIENT))


def apm_root() -> Path:
    """Session data root. Sessions are data, not source; keep them out of the repo."""
    default = Path.home() / ".local/share/7dtd-apm"
    return Path(os.environ.get("SEVENDTD_APM_DIR", default))
