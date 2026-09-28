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


def test_shipped_packages_declare_their_type_information() -> None:
    # The package is mypy strict and every module is annotated, but a wheel
    # without the PEP 561 marker is untyped to its consumers: an installed
    # apm_suite silently resolves to Any under a downstream type check, which
    # is the opposite of what the annotations are for. The marker only ships if
    # it sits inside the packaged directory and no exclude pattern covers it.
    assert (REPO / "tools" / "apm_suite" / "py.typed").is_file()
    assert "tools/apm_suite" in _patterns("wheel", "packages")
    assert "tools/apm_suite" in _patterns("sdist", "include")
    assert "py.typed" not in _patterns("wheel", "exclude")


def test_bridge_package_carries_the_license_it_ships_the_dll_under() -> None:
    # The release zip redistributes the DLL to operators' servers. Without the
    # license text in the mod folder the artifact is a binary with no stated
    # terms, and nothing in the installed tree names the license at all.
    build = (REPO / "scripts" / "build_bridge.sh").read_text(encoding="utf-8")
    assert 'cp "$ROOT/LICENSE" "$OUT/LICENSE"' in build
    assert (REPO / "LICENSE").is_file()


def test_release_zip_install_is_documented_with_its_upgrade_hazard() -> None:
    # The zip is the artifact a release attaches, and it installs by unzipping
    # over <server>/Mods/. Nothing else states that, so the install target, the
    # checksum step, and the stale-file hazard of an in-place upgrade (the one
    # install_bridge.sh prunes against) have to be written down where an
    # operator installing from the zip will read them.
    readme = (REPO / "bridge" / "README.md").read_text(encoding="utf-8")
    assert "### Installing a release zip" in readme
    assert "sha256sum -c" in readme, "the zip ships a .sha256 nobody is told to check"
    assert "unzip" in readme
    assert "/Mods/" in readme, "the zip unzips into Mods/, not into the server root"
    assert "apmbridge.json.example" in readme, "a zip install seeds no live config"
    assert "-delete" in readme, (
        "unzipping over an existing mod folder leaves files the new release "
        "dropped in place; the prune step is the documented remedy"
    )


def test_release_zip_refuses_a_dirty_working_tree() -> None:
    # `git describe` reports the exact tag for a tree carrying uncommitted
    # edits, so the ModInfo.xml check passes and a maintainer publishes an
    # archive whose bytes are not the commit the tag names. `git status` is
    # the test, and it has to be the porcelain form: `describe --dirty` misses
    # untracked files, and an untracked .cs file is globbed into the DLL.
    package = (REPO / "scripts" / "package.sh").read_text(encoding="utf-8")
    assert 'git -C "$ROOT" status --porcelain' in package
    assert "ALLOW_DIRTY" in package, "a local build needs an explicit opt-out, not a silent pass"
    guard = package.split("status --porcelain", 1)[1].split("VERSION=", 1)[0]
    assert "exit 1" in guard, "a dirty tree must stop the package before a zip is named"


def test_bridge_install_locks_the_server_and_validates_before_building() -> None:
    # Two installs against one server interleave: each backs up, prunes and
    # rolls back the other's writes. The lock has to be taken before the build
    # starts, and the Mods check has to come before the build too, so a mistyped
    # DS= fails without spending a minute compiling the DLL.
    install = (REPO / "scripts" / "install_bridge.sh").read_text(encoding="utf-8")
    assert "flock -n 9" in install, "installs against one server must be serialized"
    assert install.index("flock -n 9") < install.index('"$ROOT/scripts/build_bridge.sh"')
    assert install.index('[[ -d "$DS/Mods" ]]') < install.index('"$ROOT/scripts/build_bridge.sh"')
