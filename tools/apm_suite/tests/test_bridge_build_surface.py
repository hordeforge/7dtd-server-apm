from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

WEB_API_CS = ROOT / "bridge" / "ApmBridge" / "WebApi.cs"


def _rest_api_class_bodies(source: str) -> dict[str, str]:
    starts = [
        (match.group(1), match.start())
        for match in re.finditer(r"class\s+(\w+)\s*:\s*AbsRestApi\b", source)
    ]
    bodies = {}
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(source)
        bodies[name] = source[start:end]
    return bodies


def test_bridge_build_uses_pinned_npx_typescript() -> None:
    script = (ROOT / "scripts" / "build_bridge.sh").read_text(encoding="utf-8")
    assert 'tsc_version="${TSC_VERSION:-5.9.3}"' in script
    assert 'npx --yes -p "typescript@$tsc_version" tsc' in script
    assert "command -v tsc" not in script


def test_bridge_docs_do_not_require_global_tsc() -> None:
    docs = (ROOT / "bridge" / "README.md").read_text(encoding="utf-8")
    assert "no global `tsc`" in docs


def test_every_bridge_rest_endpoint_declares_admin_only_permissions() -> None:
    # Deny side of the web authorization matrix: the game dashboard gates each
    # REST endpoint through AdminWebModules before any handler runs, using the
    # per-method levels the class declares. Five zeros means every verb
    # (GET/POST/PUT/DELETE/other) requires permission level 0 (admin); the game
    # pads the array to 7 slots and hard-denies HEAD/OPTIONS. An endpoint that
    # drops this override inherits whatever the framework default is, so the
    # explicit declaration is mandatory for every AbsRestApi subclass here.
    bodies = _rest_api_class_bodies(WEB_API_CS.read_text(encoding="utf-8"))
    assert bodies, "no AbsRestApi subclasses found in WebApi.cs"
    for name, body in bodies.items():
        match = re.search(
            r"DefaultMethodPermissionLevels\(\)\s*(?:=>|{[^}]*?return)\s*new\[\]\s*\{([^}]*)\}",
            body,
        )
        assert match, (
            f"{name} must override DefaultMethodPermissionLevels with an "
            "explicit admin-only array (new[] { 0, 0, 0, 0, 0 })"
        )
        levels = [int(level.strip()) for level in match.group(1).split(",")]
        assert len(levels) == 5, f"{name} must declare a level for all five verbs"
        assert all(level == 0 for level in levels), (
            f"{name} declares non-admin permission levels {levels}; widening "
            "access is a deliberate security decision, not a test edit"
        )


def test_perf_post_maps_missing_config_to_conflict_not_500() -> None:
    # Contract: a missing/unreadable perf config is client-visible state (GET
    # reports available=false), so POST answers 409 UNAVAILABLE; only a real
    # write failure answers 500 WRITE_FAILED, and bad input stays 400.
    body = _rest_api_class_bodies(WEB_API_CS.read_text(encoding="utf-8"))["Perf"]
    assert 'errorCode == "WRITE_FAILED" ? HttpStatusCode.InternalServerError' in body
    assert ': errorCode == "UNAVAILABLE" ? HttpStatusCode.Conflict' in body


def test_perf_post_skips_restart_when_nothing_changed() -> None:
    # Contract: POST counts effective changes only and reports restarting=false
    # with no server shutdown on an idempotent replay; bodies with no
    # recognizable directive still answer 400 INVALID_BODY.
    body = _rest_api_class_bodies(WEB_API_CS.read_text(encoding="utf-8"))["Perf"]
    assert "restarting = changed > 0" in body
    assert "if (changed == 0) return;" in body
    assert 'directives == 0) errorCode = "INVALID_BODY"' in body


def test_apm_get_answers_coded_error_on_snapshot_failure() -> None:
    # Contract: GET /api/apm uses the same structured error envelope as the
    # perf POST (coded SendEmptyResponse) instead of an unhandled exception.
    body = _rest_api_class_bodies(WEB_API_CS.read_text(encoding="utf-8"))["Apm"]
    assert '"SNAPSHOT_FAILED"' in body
