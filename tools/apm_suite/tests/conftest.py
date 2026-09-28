"""Keep the suite off the developer's real session store.

A command invoked without an explicit --store resolves through
apm_suite.paths.apm_root(), which reads SEVENDTD_APM_DIR and falls back to
$HOME/.local/share/7dtd-server-apm. Tests that reach such a command would
write into the operator's live store (an `index` run rewrites the real
index.html) and read whatever sessions that machine happens to hold, so the
same commit passes on a clean box and fails on a working one.

Redirecting both the override and HOME per test makes the fallback a
per-test tmp dir: the default path is still exercised, it is just a
throwaway one. A test that needs a specific root sets SEVENDTD_APM_DIR
itself and overrides this.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_session_store(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sibling of tmp_path, not a child: several tests assert on the exact
    # contents of their own tmp_path.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(home / ".local/share/7dtd-server-apm"))
