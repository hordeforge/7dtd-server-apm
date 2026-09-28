#!/usr/bin/env python3
"""Regression gate: shipped versions must be consistent across sources.

Bridge mod: bridge/ApmBridge/ModInfo.xml, the Version const in
bridge/ApmBridge/BridgeMod.cs, and the "mod version" claim in
bridge/README.md must carry the same version. Same convention as
../7dtd-server-optimizer/scripts/check_version.py.

Host CLI: pyproject.toml and tools/apm_suite/__init__.py must carry the
same package version (the analyzer/session version derives from it), and
uv.lock must record that version for the root package. The lock carries the
project version too, and `uv run --locked` (every Makefile gate) fails on a
stale one, so a bump without `uv lock` breaks the build with a message that
names the lockfile, not the version.

CHANGELOG.md: the newest released section of each artifact must carry the
version that artifact actually ships, so a release cannot ship with a missing,
stale, or duplicated entry. The manifest files cannot drift from each other
without this gate noticing; without the changelog half they can still ship
with no record of what changed.

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
LOCK = ROOT / "uv.lock"
CHANGELOG = ROOT / "CHANGELOG.md"

# "## <version> - host CLI - <date>" and "## <version> (tag v<version>) - bridge mod - <date>".
# The tag suffix is optional: only tagged bridge releases carry one, and a
# section that spells the tag must spell the version it is under.
SECTION_RE = re.compile(
    r"^## (?P<version>\d+(?:\.\d+)+)"
    r"(?: \(tag v(?P<tag>\d+(?:\.\d+)+)\))? - (?P<artifact>host CLI|bridge mod)\b",
    re.M,
)
UNRELEASED_RE = re.compile(r"^## Unreleased - (?P<artifact>host CLI|bridge mod)\s*$", re.M)
ARTIFACTS = ("host CLI", "bridge mod")

# The root package's own [[package]] block: the name line is followed by the
# version, and the block ends at the next one. A dependency that happens to
# pin the same version does not match, because only the root is named exactly.
LOCK_ROOT_PACKAGE = re.compile(
    r'^\[\[package\]\]\nname = "seven-dtd-apm"\nversion = "([0-9.]+)"', re.M
)


def locked_root_version() -> str | None:
    """The version uv.lock records for this project, or None if it has none."""
    match = LOCK_ROOT_PACKAGE.search(LOCK.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def check_changelog(bridge_version: str | None, cli_version: str | None) -> list[str]:
    """The changelog must record exactly what each artifact ships.

    Catches the three ways it rots: a shipped version with no released
    section, a released section for a version that was never shipped, and a
    second "Unreleased" block for the same artifact, which splits one
    artifact's pending changes across two headings and hides the lower one
    from anything reading the file top-down.
    """
    text = CHANGELOG.read_text(encoding="utf-8")
    fails: list[str] = []
    shipped = {"host CLI": cli_version, "bridge mod": bridge_version}

    for artifact in ARTIFACTS:
        unreleased = UNRELEASED_RE.findall(text)
        count = unreleased.count(artifact)
        if count == 0:
            fails.append(f"CHANGELOG.md: no 'Unreleased - {artifact}' section")
        elif count > 1:
            fails.append(
                f"CHANGELOG.md: {count} 'Unreleased - {artifact}' sections; "
                "merge them so one artifact has one pending block"
            )

    sections = list(SECTION_RE.finditer(text))
    for artifact in ARTIFACTS:
        released = [m for m in sections if m.group("artifact") == artifact]
        if not released:
            if shipped[artifact] is not None:
                fails.append(
                    f"CHANGELOG.md: no released '{artifact}' section for shipped "
                    f"version {shipped[artifact]}"
                )
            continue
        newest = released[0]
        if shipped[artifact] is not None and newest.group("version") != shipped[artifact]:
            fails.append(
                f"CHANGELOG.md: newest '{artifact}' section is "
                f"{newest.group('version')} but {shipped[artifact]} is shipped"
            )
        for match in released:
            if match.group("tag") is not None and match.group("tag") != match.group("version"):
                fails.append(
                    f"CHANGELOG.md: '{artifact}' {match.group('version')} section is "
                    f"tagged v{match.group('tag')}"
                )

    return fails


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

    lock = locked_root_version()
    if lock is None:
        fails.append("uv.lock: no seven-dtd-apm package version")
    elif pp and lock != pp.group(1):
        fails.append(f"pyproject {pp.group(1)} != uv.lock {lock}; run `uv lock` after the bump")

    fails.extend(check_changelog(mi.group(1) if mi else None, pp.group(1) if pp else None))

    for f in fails:
        print(f"check_version: {f}", file=sys.stderr)
    if fails:
        print("check_version: FAIL", file=sys.stderr)
        return 1
    print("check_version: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
