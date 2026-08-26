#!/usr/bin/env python3
"""Regression gate: shipped versions must be consistent across sources.

Bridge mod: bridge/ApmBridge/ModInfo.xml, the Version const in
bridge/ApmBridge/BridgeMod.cs, and the "mod version" claim in
bridge/README.md must carry the same version. Same convention as
../7dtd-server-optimizer/scripts/check_version.py.

Host CLI: pyproject.toml and tools/apm_suite/__init__.py must carry the
same package version (the analyzer/session version derives from it).

Run: python3 scripts/check_version.py   (wired into `make test`)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _root() -> Path:
    """Checkout root by marker walk, not a parent count.

    Deliberately does not reuse apm_suite.paths: this gate reads the shipped
    version out of tools/apm_suite/__init__.py, so it must locate the checkout
    without importing the package whose version it is checking.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise SystemExit(f"no pyproject.toml above {__file__}; not a repository checkout")


ROOT = _root()
MODINFO = ROOT / "bridge" / "ApmBridge" / "ModInfo.xml"
BRIDGEMOD = ROOT / "bridge" / "ApmBridge" / "BridgeMod.cs"
BRIDGE_README = ROOT / "bridge" / "README.md"
PYPROJECT = ROOT / "pyproject.toml"
APM_INIT = ROOT / "tools" / "apm_suite" / "__init__.py"


def main() -> int:
    fails = []

    mi = re.search(r'Version\s+value="([0-9.]+)"', MODINFO.read_text(encoding="utf-8"))
    if mi is None:
        fails.append("ModInfo.xml: no Version value")

    cm = re.search(r'Version\s*=\s*"([0-9.]+)"', BRIDGEMOD.read_text(encoding="utf-8"))
    if cm is None:
        fails.append("BridgeMod.cs: no Version const")

    if mi and cm and mi.group(1) != cm.group(1):
        fails.append(f"ModInfo {mi.group(1)} != BridgeMod.cs Version {cm.group(1)}")

    rm = re.search(r"\bmod version\s+v?(\d+(?:\.\d+)+)", BRIDGE_README.read_text(encoding="utf-8"))
    if rm and mi and rm.group(1) != mi.group(1):
        fails.append(f"bridge/README.md claims {rm.group(1)} != ModInfo {mi.group(1)}")

    pp = re.search(r'^version\s*=\s*"(\d+(?:\.\d+)+)"', PYPROJECT.read_text(encoding="utf-8"), re.M)
    if pp is None:
        fails.append("pyproject.toml: no version")

    ai = re.search(
        r'^__version__\s*=\s*"(\d+(?:\.\d+)+)"', APM_INIT.read_text(encoding="utf-8"), re.M
    )
    if ai is None:
        fails.append("tools/apm_suite/__init__.py: no __version__")

    if pp and ai and pp.group(1) != ai.group(1):
        fails.append(f"pyproject {pp.group(1)} != apm_suite __version__ {ai.group(1)}")

    for f in fails:
        print(f"check_version: {f}", file=sys.stderr)
    if fails:
        print("check_version: FAIL", file=sys.stderr)
        return 1
    print("check_version: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
