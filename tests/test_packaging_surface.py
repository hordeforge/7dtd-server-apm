from __future__ import annotations

import tomllib

from apm_suite.paths import REPO

PYPROJECT = REPO / "pyproject.toml"


def _patterns(section: str, key: str) -> list[str]:
    with PYPROJECT.open("rb") as handle:
        targets = tomllib.load(handle)["tool"]["hatch"]["build"]["targets"]
    return list(targets[section][key])


def test_sdist_include_patterns_are_anchored() -> None:
    # Hatchling matches a bare "README.md" include by filename at any depth, so
    # an unanchored entry drags every nested README (tools/, tools/apm/,
    # tools/host_profiler/, bridge/) into the sdist the wheel does not need.
    # A directory pattern is a tree hatchling descends into; a file pattern is
    # matched by name anywhere, so only those need the leading slash.
    unanchored = [
        entry
        for entry in _patterns("sdist", "include")
        if not (REPO / entry).is_dir() and not entry.startswith("/")
    ]
    assert unanchored == [], f"unanchored sdist include patterns match at any depth: {unanchored}"


def test_shipped_targets_exclude_the_test_suite() -> None:
    for section in ("wheel", "sdist"):
        assert "tools/apm_suite/tests" in _patterns(section, "exclude"), (
            f"{section} would ship the test suite"
        )


def test_release_zip_carries_its_build_record_beside_it() -> None:
    # A DLL records neither the compiler that made it nor the game assemblies it
    # was compiled against, so the release needs the record the build already
    # knows. It stays out of the archive: the zip unzips into <server>/Mods/,
    # where an extra member installs as mod content.
    build = (REPO / "scripts" / "build_bridge.sh").read_text(encoding="utf-8")
    assert '>"$ROOT/dist/bridge-build-inputs.txt"' in build
    assert 'sha256sum "$MANAGED/Assembly-CSharp.dll"' in build
    assert '"$(dotnet --version)"' in build

    package = (REPO / "scripts" / "package.sh").read_text(encoding="utf-8")
    assert 'cp "$ROOT/dist/bridge-build-inputs.txt" "$OUT.buildinfo.txt"' in package
    staged = package.split("cp -a", 1)[1].split("\n", 1)[0]
    assert "buildinfo" not in staged, "the build record must not enter the staged mod tree"
