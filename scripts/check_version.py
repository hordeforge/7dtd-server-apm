#!/usr/bin/env python3
"""Regression gate: the shipped mod version must be consistent across sources.

Checks that bridge/ApmBridge/ModInfo.xml and the Version const in
bridge/ApmBridge/BridgeMod.cs carry the same version. Same convention as
../7dtd-optimizer/scripts/check_version.py.

Run: python3 scripts/check_version.py   (wired into `make test`)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODINFO = ROOT / "bridge" / "ApmBridge" / "ModInfo.xml"
BRIDGEMOD = ROOT / "bridge" / "ApmBridge" / "BridgeMod.cs"


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

    for f in fails:
        print(f"check_version: {f}", file=sys.stderr)
    if fails:
        print("check_version: FAIL", file=sys.stderr)
        return 1
    print("check_version: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
