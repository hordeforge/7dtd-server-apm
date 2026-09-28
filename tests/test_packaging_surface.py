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
