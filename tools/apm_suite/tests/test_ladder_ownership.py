"""Session ownership for the scale ladder (plans/scale_ladder.py).

The ladder attaches its workload.json to the session its own capture created.
A repeat run, a second operator, or a scheduled capture all produce sessions
inside the same window, so "newest session" is not proof of ownership and
writing into a session this run did not create invalidates its recorded
manifest hashes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from apm_suite.paths import REPO


def _ladder() -> ModuleType:
    """plans/scale_ladder.py as a module.

    A lab script outside the package, loaded by path: it is not importable as
    apm_suite.* and must not become an import just for a test.
    """
    path = REPO / "plans" / "scale_ladder.py"
    spec = importlib.util.spec_from_file_location("scale_ladder", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["scale_ladder"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ladder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[ModuleType, Path]:
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("SEVENDTD_APM_DIR", str(store))
    module = _ladder()
    return module, store


def _session(store: Path, name: str) -> Path:
    created = store / name
    created.mkdir()
    return created


def test_exactly_the_new_session_is_returned(ladder: tuple[ModuleType, Path]) -> None:
    module, store = ladder
    existing = _session(store, "session_20260928_100000")
    before = module.session_names()
    created = _session(store, "session_20260928_110000")
    assert module.new_sessions(before) == [created]
    assert existing not in module.new_sessions(before)


def test_a_capture_that_created_nothing_returns_nothing(ladder: tuple[ModuleType, Path]) -> None:
    module, store = ladder
    _session(store, "session_20260928_100000")
    before = module.session_names()
    assert module.new_sessions(before) == []


def test_a_concurrent_capture_is_not_attributable(ladder: tuple[ModuleType, Path]) -> None:
    # Two new sessions: one is ours, one belongs to a scheduled or second-run
    # capture. Picking either by mtime is a guess, and the ladder's contract is
    # to attach nothing rather than write into a session it cannot prove it
    # created (the caller requires exactly one).
    module, store = ladder
    before = module.session_names()
    mine = _session(store, "session_20260928_110000")
    theirs = _session(store, "session_20260928_110001")
    fresh = module.new_sessions(before)
    assert set(fresh) == {mine, theirs}
    assert len(fresh) != 1
