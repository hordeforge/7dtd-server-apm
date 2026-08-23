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
    # The pin lives in scripts/lib/tool_versions.sh, shared with lint-webui.sh
    # so the freshness gate checks the same TypeScript that ships.
    script = (ROOT / "scripts" / "build_bridge.sh").read_text(encoding="utf-8")
    assert "scripts/lib/tool_versions.sh" in script
    assert 'npx --yes -p "typescript@$TSC_VERSION" tsc' in script
    assert "command -v tsc" not in script

    lib = (ROOT / "scripts" / "lib" / "tool_versions.sh").read_text(encoding="utf-8")
    match = re.search(r':\s*"\$\{TSC_VERSION:=([0-9.]+)\}"', lib)
    assert match, "tool_versions.sh must pin TSC_VERSION to an explicit x.y.z"
    assert re.fullmatch(r"\d+\.\d+\.\d+", match.group(1))


def test_bridge_docs_do_not_require_global_tsc() -> None:
    docs = (ROOT / "bridge" / "README.md").read_text(encoding="utf-8")
    assert "no global `tsc`" in docs


def test_release_zip_ships_example_config_not_live_config() -> None:
    # Upgrade contract: users install a release by unzipping it over Mods/,
    # which overwrites every archive member. The zip must therefore carry only
    # Config/apmbridge.json.example; shipping the live config name would reset
    # operator-tuned settings on every upgrade (the mod runs on built-in
    # defaults when the file is absent).
    build = (ROOT / "scripts" / "build_bridge.sh").read_text(encoding="utf-8")
    assert '"$OUT/Config/apmbridge.json.example"' in build
    assert 'cp "$ROOT/bridge/ApmBridge/apmbridge.json" "$OUT/Config/apmbridge.json"' not in build

    package = (ROOT / "scripts" / "package.sh").read_text(encoding="utf-8")
    assert "Config/apmbridge.json" in package, (
        "package.sh must keep excluding the live config name from the stage"
    )

    install = (ROOT / "scripts" / "install_bridge.sh").read_text(encoding="utf-8")
    assert "Config/apmbridge.json.example" in install
    assert install.index("if [[ ! -f") < install.index("apmbridge.json.example"), (
        "first-install seeding must stay conditional on no existing config"
    )


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
