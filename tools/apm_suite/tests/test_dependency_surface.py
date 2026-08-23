from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

HEREDOC_START = re.compile(r"<<-?(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


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
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
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
    # npx fetches and executes whatever the registry currently serves, so every
    # invocation in repo scripts must pin package@version. Heredoc bodies are
    # documentation (e.g. the speedscope hint in OPEN_FLAMES.txt), not runs.
    script_dirs = [
        ROOT / "scripts",
        ROOT / "tools" / "host_profiler",
        ROOT / "tools" / "apm",
    ]
    scripts = sorted(path for directory in script_dirs for path in directory.rglob("*.sh"))
    assert scripts, "no shell scripts found"
    unpinned: list[str] = []
    versioned = re.compile(r"[\"']?[A-Za-z0-9._/@-]+@[A-Za-z0-9.$_{}-]+")
    # A command position: optional indentation/env assignments, then `npx`
    # (excludes `command -v npx`, echo strings mentioning npx, etc.).
    invocation = re.compile(r"^\s*(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*npx(?:\s|$)")
    for path in scripts:
        body = _strip_shell_heredocs(path.read_text(encoding="utf-8"))
        for lineno, line in enumerate(body.splitlines(), start=1):
            if not invocation.match(line):
                continue
            if not versioned.search(line):
                unpinned.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not unpinned, (
        f"npx calls must pin package@version (override vars like TSC_VERSION count): {unpinned}"
    )
