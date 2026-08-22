from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_bridge_build_uses_pinned_npx_typescript() -> None:
    script = (ROOT / "scripts" / "build_bridge.sh").read_text(encoding="utf-8")
    assert 'tsc_version="${TSC_VERSION:-5.9.3}"' in script
    assert 'npx --yes -p "typescript@$tsc_version" tsc' in script
    assert "command -v tsc" not in script


def test_bridge_docs_do_not_require_global_tsc() -> None:
    docs = (ROOT / "bridge" / "README.md").read_text(encoding="utf-8")
    assert "no global `tsc`" in docs
