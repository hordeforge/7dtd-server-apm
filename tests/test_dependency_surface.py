from __future__ import annotations

import ast
import re
import sys
import tomllib
from typing import Any

from apm_suite.paths import REPO

HEREDOC_START = re.compile(r"<<-?(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

PYPROJECT = REPO / "pyproject.toml"
LOCKFILE = REPO / "uv.lock"

# The four Python roots mypy already gates. A manifest check that scans fewer
# roots than the type gate would miss exactly the file a new dependency lands
# in, so the two are held to the same list.
PYTHON_ROOTS = ("tools", "scripts", "plans", "tests")

# Declared distributions with no import site, each for a stated reason. This is
# an allowlist, not an escape hatch: an entry is a claim about how the
# distribution is used, so removing the reason means the entry goes too. Every
# other declared distribution must be imported by name somewhere in the tree,
# or it is dead weight on every install and every `uv sync`.
NOT_IMPORTED_BY_DESIGN: dict[str, str] = {
    "hatchling": "PEP 517 build backend; the build frontend imports it, repo source never does",
    "mypy": "console script run as `uv run mypy`, never imported by name",
    "ruff": "console script run as `uv run ruff`, never imported by name",
    "pytest-cov": "pytest plugin discovered through its entry point, never imported by name",
    "types-psutil": "stub-only distribution; it types the psutil runtime dep and ships no module",
}


def _strip_shell_heredocs(text: str) -> str:
    """Blank out heredoc bodies so hint text (e.g. OPEN_FLAMES.txt notes) is
    not mistaken for executed commands."""
    out: list[str] = []
    terminator: str | None = None
    for line in text.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
                out.append("")
            continue
        match = HEREDOC_START.search(line)
        if match:
            terminator = match.group(2)
        out.append(line)
    return "\n".join(out)


def test_ci_actions_are_pinned_to_commit_shas() -> None:
    # Mutable tags (actions/checkout@v4) can be force-moved after review; CI
    # must execute immutable SHAs. Dependabot keeps the pins current.
    workflows = sorted((REPO / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no GitHub Actions workflows found"
    for path in workflows:
        refs = re.findall(r"uses:\s*(\S+)", path.read_text(encoding="utf-8"))
        assert refs, f"{path.name} declares no actions"
        for ref in refs:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), (
                f"{path.name}: {ref} must be pinned to a full commit SHA "
                "(owner/repo@<40 hex>), not a mutable branch/tag"
            )


def test_executed_npx_calls_are_version_pinned() -> None:
    # npx and bunx both fetch and execute whatever the registry currently
    # serves, so every invocation in repo scripts must pin package@version.
    # bunx is the runner the build actually uses (scripts/build_bridge.sh,
    # lint-webui.sh, lint-html.sh), so guarding npx alone would leave the real
    # path uncovered. Heredoc bodies are documentation (e.g. the speedscope
    # hint in OPEN_FLAMES.txt), not runs.
    script_dirs = [
        REPO / "scripts",
        REPO / "tools" / "host_profiler",
        REPO / "tools" / "apm",
    ]
    scripts = sorted(path for directory in script_dirs for path in directory.rglob("*.sh"))
    assert scripts, "no shell scripts found"
    unpinned: list[str] = []
    versioned = re.compile(r"[\"']?[A-Za-z0-9._/@-]+@[A-Za-z0-9.$_{}-]+")
    # A command position: optional indentation/env assignments, then the
    # runner (excludes `command -v npx`, echo strings mentioning npx, etc.).
    invocation = re.compile(
        r"^\s*(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:npx|bunx)(?:\s|$)"
    )
    for path in scripts:
        body = _strip_shell_heredocs(path.read_text(encoding="utf-8"))
        for lineno, line in enumerate(body.splitlines(), start=1):
            if not invocation.match(line):
                continue
            if not versioned.search(line):
                unpinned.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not unpinned, (
        f"npx/bunx calls must pin package@version (override vars like TSC_VERSION count): {unpinned}"
    )


def _pyproject() -> dict[str, Any]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def _dist_name(requirement: str) -> str:
    # "psutil[slim]>=7,<8 ; sys_platform == 'linux'" -> "psutil". The leading
    # name is everything before the first operator, bracket or space.
    head = requirement.split(";", 1)[0].strip()
    return re.sub(r"[-_.]+", "-", re.split(r"[\s<>=!~\[]", head, maxsplit=1)[0]).lower()


def _declared() -> dict[str, str]:
    """Distribution name -> the pyproject section that declares it."""
    data = _pyproject()
    declared: dict[str, str] = {}
    for requirement in data["project"]["dependencies"]:
        declared[_dist_name(requirement)] = "project.dependencies"
    for group, requirements in data["dependency-groups"].items():
        for requirement in requirements:
            declared[_dist_name(requirement)] = f"dependency-groups.{group}"
    for requirement in data["build-system"]["requires"]:
        declared[_dist_name(requirement)] = "build-system.requires"
    assert declared, "pyproject declares no dependencies at all"
    return declared


def _local_modules() -> set[str]:
    """Top-level module and package names this repo owns.

    Derived from the tree rather than listed by hand: a hand-maintained list
    drifts, and the next new local module would then be read as an undeclared
    third-party import and send the gate chasing a dependency that does not
    exist.
    """
    names: set[str] = set()
    for root in PYTHON_ROOTS:
        for path in (REPO / root).rglob("*"):
            if path.suffix == ".py":
                names.add(path.stem)
            elif path.is_dir() and (path / "__init__.py").is_file():
                names.add(path.name)
    assert names, "no Python sources found under the scanned roots"
    return names


def _imports() -> dict[str, set[str]]:
    """Top-level imported module name -> relative paths importing it.

    An import inside a function body counts: this repo defers psutil, jinja2
    and the reporting chain to command scope to keep CLI startup fast, and
    those call sites are exactly the ones a module-level-only scan would miss
    and then report as unused dependencies.
    """
    found: dict[str, set[str]] = {}
    for root in PYTHON_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        found.setdefault(alias.name.split(".")[0], set()).add(
                            path.relative_to(REPO).as_posix()
                        )
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    # node.module is None only for `from . import x`, which is
                    # relative and already excluded by the level check.
                    root_module = (node.module or "").split(".")[0]
                    if root_module:
                        found.setdefault(root_module, set()).add(path.relative_to(REPO).as_posix())
    assert found, "no imports parsed from the tree"
    return found


def test_every_third_party_import_is_a_declared_dependency() -> None:
    # An import nothing declares is an installed-but-undeclared dependency:
    # it works only because something else happens to pull it in, so a future
    # `uv lock` that drops that edge breaks the install with no manifest diff
    # to explain it. Transitive imports fail here too, which is the point: the
    # project's rule is that everything beyond the declared five is stdlib.
    declared = set(_declared())
    local = _local_modules()
    undeclared = {
        f"{module} ({sorted(sites)[0]})"
        for module, sites in _imports().items()
        if module not in sys.stdlib_module_names
        and module not in local
        and module.replace("_", "-").lower() not in declared
    }
    assert not undeclared, f"imported but not declared in pyproject.toml: {sorted(undeclared)}"


def test_every_declared_dependency_is_imported_or_justified() -> None:
    # The other direction: a declared package nothing imports is installed on
    # every `uv sync` and every release, widening the supply-chain surface for
    # code that never runs. Stubs, plugins and console scripts have no import
    # site by nature, so they are named in NOT_IMPORTED_BY_DESIGN with the
    # reason; anything else has to show up in the tree.
    imported = {module.replace("_", "-").lower() for module in _imports()}
    unjustified = sorted(
        name for name in _declared() if name not in imported and name not in NOT_IMPORTED_BY_DESIGN
    )
    assert not unjustified, (
        f"declared but never imported: {unjustified}. Remove the requirement, or "
        f"add it to NOT_IMPORTED_BY_DESIGN with the reason it has no import site."
    )
    stale = sorted(set(NOT_IMPORTED_BY_DESIGN) - set(_declared()))
    assert not stale, (
        f"NOT_IMPORTED_BY_DESIGN names a distribution that is no longer declared: {stale}"
    )


def test_every_declared_dependency_is_bounded_on_both_ends() -> None:
    # A bare `>=` lets the next `uv lock` pull an unreviewed major into a
    # release without a diff in pyproject.toml, which is the failure this
    # manifest's comment block is written to prevent. A floor says what is
    # tested, a ceiling says what a new major has to be resolved against.
    data = _pyproject()
    requirements: list[tuple[str, str]] = []
    for requirement in data["project"]["dependencies"]:
        requirements.append(("project.dependencies", requirement))
    for group, group_requirements in data["dependency-groups"].items():
        requirements.extend((f"dependency-groups.{group}", item) for item in group_requirements)
    for requirement in data["build-system"]["requires"]:
        requirements.append(("build-system.requires", requirement))

    lower = {">=", "~=", "=="}
    upper = {"<=", "<", "=="}
    unbounded: list[str] = []
    for section, requirement in requirements:
        specifier = requirement.split(";", 1)[0]
        operators = set(re.findall(r"===|==|~=|!=|<=|>=|<|>", specifier))
        if not (operators & lower) or not (operators & upper):
            unbounded.append(f"{section}: {requirement.strip()}")
    assert not unbounded, (
        f"requirements need a floor and a ceiling so a lock refresh cannot "
        f"introduce a new major unreviewed: {unbounded}"
    )


def test_every_locked_artifact_is_hash_pinned() -> None:
    # uv verifies a registry artifact against the sha256 in the lock, so an
    # un-hashed entry is the one path where a compromised or re-uploaded file
    # would install without detection. The editable project root and any
    # path/git source have no registry artifacts to verify, so they are out of
    # scope here.
    data = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))
    assert LOCKFILE.is_file(), "uv.lock must be committed"
    unverified: list[str] = []
    for package in data["package"]:
        if "registry" not in (package.get("source") or {}):
            continue
        artifacts = [package["sdist"]] if "sdist" in package else []
        artifacts += package.get("wheels", [])
        name = package["name"]
        if not artifacts:
            unverified.append(f"{name}: locked from the registry with no artifact recorded")
            continue
        unverified += [
            f"{name}: {artifact['url']} carries no sha256"
            for artifact in artifacts
            if not str(artifact.get("hash", "")).startswith("sha256:")
        ]
    assert not unverified, f"uv.lock entries without verified hashes: {unverified}"


def test_lock_agrees_with_the_manifest_interpreter_floor() -> None:
    # The lock is resolved for one requires-python. A manifest that moves the
    # floor without re-locking leaves `uv sync --locked` resolving for an
    # interpreter range the code no longer claims to support.
    data = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))
    declared_floor = _pyproject()["project"]["requires-python"]
    assert data["requires-python"] == declared_floor, (
        f"uv.lock pins requires-python {data['requires-python']} but pyproject "
        f"declares {declared_floor}; run `uv lock`"
    )
