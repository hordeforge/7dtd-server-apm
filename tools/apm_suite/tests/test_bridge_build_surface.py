from __future__ import annotations

import re

from apm_suite.paths import REPO

WEB_API_CS = REPO / "bridge" / "ApmBridge" / "WebApi.cs"
TELEMETRY_CS = REPO / "bridge" / "ApmBridge" / "Telemetry.cs"


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


def test_bridge_build_uses_pinned_bunx_typescript() -> None:
    # The pin lives in scripts/lib/tool_versions.sh, shared with lint-webui.sh
    # so the freshness gate checks the same TypeScript that ships.
    script = (REPO / "scripts" / "build_bridge.sh").read_text(encoding="utf-8")
    assert "scripts/lib/tool_versions.sh" in script
    assert 'bunx -p "typescript@$TSC_VERSION" tsc' in script
    assert "command -v tsc" not in script

    lib = (REPO / "scripts" / "lib" / "tool_versions.sh").read_text(encoding="utf-8")
    match = re.search(r':\s*"\$\{TSC_VERSION:=([0-9.]+)\}"', lib)
    assert match, "tool_versions.sh must pin TSC_VERSION to an explicit x.y.z"
    assert re.fullmatch(r"\d+\.\d+\.\d+", match.group(1))


def test_bridge_docs_do_not_require_global_tsc() -> None:
    docs = (REPO / "bridge" / "README.md").read_text(encoding="utf-8")
    assert "no global `tsc`" in docs


def test_release_zip_ships_example_config_not_live_config() -> None:
    # Upgrade contract: users install a release by unzipping it over Mods/,
    # which overwrites every archive member. The zip must therefore carry only
    # Config/apmbridge.json.example; shipping the live config name would reset
    # operator-tuned settings on every upgrade (the mod runs on built-in
    # defaults when the file is absent).
    build = (REPO / "scripts" / "build_bridge.sh").read_text(encoding="utf-8")
    assert '"$OUT/Config/apmbridge.json.example"' in build
    assert 'cp "$ROOT/bridge/ApmBridge/apmbridge.json" "$OUT/Config/apmbridge.json"' not in build

    package = (REPO / "scripts" / "package.sh").read_text(encoding="utf-8")
    assert "Config/apmbridge.json" in package, (
        "package.sh must keep excluding the live config name from the stage"
    )

    install = (REPO / "scripts" / "install_bridge.sh").read_text(encoding="utf-8")
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


def test_apm_get_answers_coded_error_on_snapshot_failure() -> None:
    # Contract: GET /api/apm answers a structured error envelope (coded
    # SendEmptyResponse) instead of an unhandled exception.
    body = _rest_api_class_bodies(WEB_API_CS.read_text(encoding="utf-8"))["Apm"]
    assert '"SNAPSHOT_FAILED"' in body


def test_apm_get_sends_the_snapshot_before_the_error_envelope() -> None:
    # Ordering: a failed build must answer the coded error, a successful one the
    # document itself. A handler that wrote the snapshot before guarding the
    # build would return a 200 carrying partial evidence on the error path.
    body = _rest_api_class_bodies(WEB_API_CS.read_text(encoding="utf-8"))["Apm"]
    assert body.index("SnapshotJson()") < body.index("SendEmptyResponse")
    assert body.index("SendEmptyResponse") < body.index("SendEnvelopedResult")


def _emitted_section_fields() -> set[str]:
    """Field names of the anonymous object Telemetry.Metric.Build returns."""
    source = TELEMETRY_CS.read_text(encoding="utf-8")
    body = source[source.index("public static object Build(Copied c)") :]
    literal = re.search(r"return new \{(.*?)\};", body, re.DOTALL)
    assert literal, "Metric.Build no longer returns an anonymous object"
    text = re.sub(r"//[^\n]*", "", literal.group(1))
    return set(re.findall(r"(\w+)\s*=", text))


def test_managed_section_model_does_not_require_unemitted_fields() -> None:
    # Consumer side of the bridge contract: a snapshot section is validated by
    # ManagedSectionV3, whose non-defaulted fields are mandatory. A field the
    # model requires but the bridge never emits rejects every real snapshot at
    # ingestion ("bridge snapshot rejected by schema validation"), so the two
    # sides must be changed together.
    from apm_suite.models import ManagedSectionV3

    emitted = _emitted_section_fields()
    required = {
        name for name, field in ManagedSectionV3.model_fields.items() if field.is_required()
    }
    assert required <= emitted, (
        f"ManagedSectionV3 requires {sorted(required - emitted)}; the bridge "
        "snapshot does not emit them"
    )


def test_snapshot_utc_fields_carry_no_placeholder_string() -> None:
    # Documented response contract: every utc field is an ISO-8601 instant or
    # null. A sentinel string in a date slot makes a client parse "unavailable"
    # as a timestamp; absence is the only honest marker before the first sample.
    source = TELEMETRY_CS.read_text(encoding="utf-8")
    assert not re.search(r"utc\s*=\s*\"(?!\{)", source), (
        "a utc field is initialized from a string literal; use null instead"
    )


def test_bridge_readme_documents_the_response_contract() -> None:
    # The endpoint's payload, status codes, and error code are the API contract
    # for both the dashboard panel and any external scraper; the docs must
    # state them, not just the authorization matrix.
    readme = (REPO / "bridge" / "README.md").read_text(encoding="utf-8")
    assert "### Response contract" in readme
    assert "SNAPSHOT_FAILED" in readme
    for key in (
        "capabilities",
        "measurement",
        "update",
        "health",
        "host",
        "gc",
        "world",
        "mapTransfers",
        "sections",
        "spikes",
    ):
        assert f"| `{key}`" in readme, f"response contract does not document {key}"
