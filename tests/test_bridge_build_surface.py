from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from apm_suite.paths import REPO

WEB_API_CS = REPO / "bridge" / "ApmBridge" / "WebApi.cs"
TELEMETRY_CS = REPO / "bridge" / "ApmBridge" / "Telemetry.cs"
DOTNET_SDK_SH = REPO / "scripts" / "lib" / "dotnet_sdk.sh"
BUILD_BRIDGE_SH = REPO / "scripts" / "build_bridge.sh"
GLOBAL_JSON = REPO / "global.json"


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


def test_bridge_build_enforces_the_sdk_pin_from_global_json() -> None:
    # global.json is the only place the SDK version is written, and the muxer
    # resolves it from the working directory while merely warning when the pin
    # is unmet. A build that took whichever dotnet it found first, or that
    # passed an absolute project path from an arbitrary cwd, would compile the
    # shipped DLL with a compiler no file in the repo names.
    assert re.fullmatch(
        r"\d+\.\d+\.\d+", json.loads(GLOBAL_JSON.read_text(encoding="utf-8"))["sdk"]["version"]
    )
    script = BUILD_BRIDGE_SH.read_text(encoding="utf-8")
    assert "scripts/lib/dotnet_sdk.sh" in script
    assert 'dotnet_use_pinned_sdk "$ROOT"' in script
    assert 'cd "$ROOT" &&\n    dotnet build bridge/ApmBridge/ApmBridge.csproj' in script
    for line in script.splitlines():
        if "dotnet build" in line:
            assert "$ROOT/bridge" not in line, (
                "an absolute project path ignores global.json, which dotnet "
                f"resolves from the working directory: {line.strip()}"
            )


def _sdk_satisfies_pin(resolved: str, pinned: str) -> bool:
    """Run the sourced fragment's predicate; exit status is the answer."""
    return (
        subprocess.run(
            [
                "bash",
                "-c",
                '. "$1" >/dev/null; dotnet_sdk_matches_pin "$2" "$3"',
                "_",
                *map(str, (DOTNET_SDK_SH, resolved, pinned)),
            ],
            check=False,
        ).returncode
        == 0
    )


def test_sdk_pin_rolls_forward_within_the_feature_band_only() -> None:
    # global.json's rollForward is latestPatch: a newer patch of the same
    # feature band is the same compiler contract, a different band or an older
    # patch is not. Accepting those would compile the release with a toolchain
    # the pin does not name; rejecting a newer patch would break the build for
    # no gain, since a security-patch bump does not change codegen.
    assert _sdk_satisfies_pin("8.0.423", "8.0.423")
    assert _sdk_satisfies_pin("8.0.500", "8.0.423")
    assert not _sdk_satisfies_pin("8.0.100", "8.0.423")
    assert not _sdk_satisfies_pin("8.1.100", "8.0.423")
    assert not _sdk_satisfies_pin("9.0.100", "8.0.423")
    assert _sdk_satisfies_pin("9.9.9", ""), "no pin in global.json means nothing to enforce"


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


def test_bridge_uninstall_keeps_every_config_it_ever_saved() -> None:
    # Rerun property: install -> uninstall -> install -> uninstall must not
    # destroy the tuned config the first uninstall preserved. The reinstall
    # seeds a fresh factory apmbridge.json, so a second uninstall that moves
    # it onto the saved name with -f overwrites the operator's settings with
    # defaults that no archive in this repo carries.
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    uninstall = makefile.split("bridge-uninstall:", 1)[1].split("\npackage:", 1)[0]
    assert "mv -f" not in uninstall, (
        "bridge-uninstall must not overwrite a config an earlier uninstall saved"
    )
    assert "7dtd-server-apm-bridge-config.json" in uninstall
    assert '[ -e "$$dest" ]' in uninstall, (
        "the saved-config target must be probed for a free name, not overwritten"
    )


def test_lint_webui_extraction_leaves_no_half_populated_cache() -> None:
    # Same rerun property for the vendored plugin cache: the fetch is guarded
    # by `[ ! -d "$anti_slop_dir" ]`, so an extraction that dies midway (disk
    # full, interrupted tar) would leave a directory every later run accepts as
    # a populated cache and never retries. The rename-into-place keeps a failed
    # run leaving no such directory behind.
    script = (REPO / "scripts" / "lint-webui.sh").read_text(encoding="utf-8")
    bootstrap = script.split('if [ ! -d "$anti_slop_dir" ]; then', 1)[1]
    extract = bootstrap.split("tar xzf", 1)[1].split("\nfi", 1)[0]
    assert 'mv "$staging" "$anti_slop_dir"' in extract, (
        "the cache dir must be created by renaming a completed extraction"
    )
    assert 'mkdir -p "$anti_slop_dir"\n' not in bootstrap, (
        "the cache dir must not be created in place before the extract succeeds"
    )


def test_lint_webui_plugin_cache_is_keyed_by_the_pinned_commit() -> None:
    # ANTI_SLOP_SHA is meant to be bumped. A cache dir named only for the
    # package would then serve the previous commit's rules forever and skip
    # ANTI_SLOP_SHA256 verification on every later run, so the bump would
    # silently not take effect.
    script = (REPO / "scripts" / "lint-webui.sh").read_text(encoding="utf-8")
    assert 'anti_slop_dir="$cache_dir/anti-slop-$anti_slop_sha"' in script
    assert "anti-slop-src" not in script, "the unversioned cache name must be gone"


def test_shipped_webmod_assets_stay_inside_the_download_budget() -> None:
    # The stock dashboard loads every webmod bundle.js and styling.css on every
    # page, so their bytes are the panel's whole download cost. Budgets, not
    # aspirations: a feature that needs more raises the number here with the
    # measurement that justified it.
    webmod = REPO / "bridge" / "ApmBridge" / "WebMod"
    budgets = {"bundle.js": 36_000, "styling.css": 12_000}
    for name, budget in budgets.items():
        size = (webmod / name).stat().st_size
        assert size <= budget, f"{name} is {size} B, over the {budget} B budget"


def test_emitted_webmod_bundle_carries_no_comments() -> None:
    # tsconfig emits with removeComments, so the source comments in bundle.ts
    # (10.5 KB of a 44 KB emit) do not ride along on every dashboard load. The
    # freshness gate in lint-webui.sh only compares a fresh tsc run, so it would
    # not notice someone flipping the flag back on.
    tsconfig = (REPO / "bridge" / "ApmBridge" / "WebMod" / "tsconfig.json").read_text(
        encoding="utf-8"
    )
    assert '"removeComments": true' in tsconfig
    bundle = (REPO / "bridge" / "ApmBridge" / "WebMod" / "bundle.js").read_text(encoding="utf-8")
    assert "//" not in bundle, "the emitted bundle still carries a line comment"
    assert bundle.startswith('"use strict";'), (
        "the directive must survive comment removal: the panel relies on strict mode"
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


def test_apm_get_answers_a_live_snapshot_as_uncacheable() -> None:
    # Contract: the 200 body is sampled when the request arrives and the panel
    # repolls it every 2 s, so it is not a cacheable representation of a URL. A
    # caching proxy ahead of the dashboard port may otherwise store a response
    # that carries no freshness of its own and replay a stale snapshot.
    body = _rest_api_class_bodies(WEB_API_CS.read_text(encoding="utf-8"))["Apm"]
    assert 'Headers["Cache-Control"] = "no-store"' in body, (
        "the snapshot must be marked no-store before it is sent"
    )
    assert body.index('"no-store"') < body.index("SendEnvelopedResult"), (
        "the header must be set before any response is written"
    )


def _telemetry_source() -> str:
    return TELEMETRY_CS.read_text(encoding="utf-8")


def test_api_request_path_is_counted_timed_and_logged_with_context() -> None:
    # The panel polls GET /api/apm every 2 s, so the endpoint is a production
    # request path: a failure has to be countable, timed, and logged with the
    # stack trace, since the coded error envelope carries no detail.
    body = _rest_api_class_bodies(WEB_API_CS.read_text(encoding="utf-8"))["Apm"]
    assert "Telemetry.ApiSnapshotJson()" in body, (
        "the handler must go through the instrumented entry point"
    )
    assert "apm snapshot failed" not in body, (
        "the failure is logged once, where the exception is still in scope"
    )
    source = _telemetry_source()
    timed = source[source.index("public static string ApiSnapshotJson()") :]
    timed = timed[: timed.index("public static string Dump()")]
    for needle in (
        "Interlocked.Increment(ref _apiRequests)",
        "Interlocked.Increment(ref _apiErrors)",
        "ApiMetric.Add(",
        "throw;",
    ):
        assert needle in timed, f"ApiSnapshotJson must contain {needle}"
    assert 'BridgeMod.Log("apm snapshot failed: " + ex);' in timed, (
        "the failure log must carry the exception (type, message, stack trace)"
    )
    assert "catch (Exception ex)\n            {" in timed and "ex.Message" in timed, (
        "the failure must be named in the payload, not only in the log"
    )


def test_snapshot_reports_the_api_section_and_per_source_errors() -> None:
    # An operator reads one document: the endpoint's latency belongs in
    # sections (so the percentiles match every other measured section) and the
    # failure counts in health, next to the other unmeasured-field errors.
    source = _telemetry_source()
    health = re.search(r"health = new \{(.*?)\};", source, re.DOTALL)
    assert health
    fields = set(re.findall(r"(\w+)\s*=", health.group(1)))
    assert {
        "apiRequests",
        "apiErrors",
        "lastApiError",
        "lastExportError",
        "lastSampleError",
        "hostError",
    } <= fields, f"health is missing {sorted(fields)}"


def test_unmeasurable_fields_do_not_share_one_error_slot() -> None:
    # A single shared slot let a successful export clear a world-sample error
    # another thread had just recorded, so a field that was never measured
    # read as healthy. Each source owns a field and only its own next success
    # may clear it.
    source = _telemetry_source()
    slots = ("_lastExportError", "_lastSampleError", "_lastHostError", "_lastApiError")
    for slot in slots:
        assert f"volatile string {slot}" in source, f"{slot} must be a volatile field"
    sampling = source[source.index("static WorldSample SampleWorld()") :]
    sampling = sampling[: sampling.index("public const int DashboardSpikeRecords")]
    assert "_lastExportError" not in sampling, (
        "the world sample must report into its own slot, not the export's"
    )
    host = source[source.index("static object HostSample()") :]
    host = host[: host.index("static long ParseMeminfo")]
    assert "_lastExportError" not in host, (
        "a failed /proc read must report into its own slot, not the export's"
    )
    assert '_lastHostError = "";' in host, "a successful host read clears its own error"
    # The host read runs per snapshot (the panel polls every 2 s); a persistently
    # unreadable /proc logged once per poll for the process lifetime.
    assert 'if (_lastHostError != detail) BridgeMod.Log("host sample failed: " + detail);' in host
    transfer = BRIDGE_MOD_CS.read_text(encoding="utf-8")
    postfix = transfer[transfer.index("public static void MapTransferPostfix(") :]
    postfix = postfix[: postfix.index("static string Describe(")]
    assert 'if (_mapTransferError != detail) Log("map transfer counter failed: " + detail);' in (
        postfix
    ), "a failing per-package counter must not log once per network package"


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
    assert "Cache-Control: no-store" in readme, (
        "the response contract does not document the no-store header the handler sets"
    )


def test_bridge_releases_every_os_handle_it_acquires() -> None:
    # The mod runs inside the dedicated server for weeks, and `apm jitmap` fires
    # on every --symbolize capture. A Process/FileStream/StreamWriter taken
    # without `using` holds a real OS handle until the finalizer runs, so each
    # invocation leaks one against a handle the host is already near its limit
    # for. Every acquisition site must be scoped.
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((REPO / "bridge" / "ApmBridge").glob("*.cs"))
    }
    assert sources, "no bridge sources found"
    factories = (
        "Process.GetCurrentProcess(",
        "new Process(",
        "File.OpenRead(",
        "new FileStream(",
        "new StreamWriter(",
        "new StreamReader(",
    )
    pattern = "|".join(re.escape(factory) for factory in factories)
    for name, source in sources.items():
        for match in re.finditer(pattern, source):
            line = source.count("\n", 0, match.start()) + 1
            assert "using" in source[max(0, match.start() - 200) : match.start()], (
                f"{name}:{line} acquires a disposable handle outside a using block"
            )


CONFIG_CS = REPO / "bridge" / "ApmBridge" / "BridgeConfig.cs"
BRIDGE_MOD_CS = REPO / "bridge" / "ApmBridge" / "BridgeMod.cs"
EXAMPLE_CONFIG = REPO / "bridge" / "ApmBridge" / "apmbridge.json"


def test_repeated_dumps_never_resolve_to_one_file() -> None:
    # `apm dump` is a second-resolution name by design, and the timestamped
    # prune depends on that ordering. Two dumps inside one second (a retried
    # capture, an operator repeating a lost command) must produce two files:
    # a shared path makes the second publish replace the first dump's evidence
    # while reporting it as written.
    source = _telemetry_source()
    body = re.search(r"static string UniqueDumpPath\(\)(.*?)\n        \}", source, re.DOTALL)
    assert body, "Dump must pick its path through a collision-aware helper"
    helper = body.group(1)
    assert "File.Exists(path)" in helper, "the loop must stop on the first free name"
    assert helper.index("File.Exists(path)") < helper.rindex("return path")
    dump = re.search(r"public static string Dump\(\)(.*?)\n        \}", source, re.DOTALL)
    assert dump and "UniqueDumpPath()" in dump.group(1)
    # Chronological order the prune relies on: the timestamp stays the prefix.
    assert '"apm_app_" + stamp' in helper
    assert '"apm_app_" + stamp + "_" + i + ".json"' in helper
    # And the prune still reaches the suffixed names.
    prune = re.search(
        r"static void PruneTimestampedDumps\(int keep\)(.*?)\n        \}", source, re.DOTALL
    )
    assert prune and '"apm_app_2*.json"' in prune.group(1)


def _console_cmd_body() -> str:
    """Source of the `apm` console command class."""
    source = BRIDGE_MOD_CS.read_text(encoding="utf-8")
    return source[source.index("class ConsoleCmdApm") :]


def test_console_verb_table_is_the_only_source_of_the_usage_line() -> None:
    # The help line a caller reads and the verbs the dispatcher accepts were two
    # copies of the same list: a verb added to one and not the other was
    # documented as unavailable (or silently undocumented). Both read the table.
    body = _console_cmd_body()
    table = re.search(r"static readonly Verb\[\] Verbs = \{(.*?)\};", body, re.DOTALL)
    assert table, "the verb list must be a table the usage line and dispatch read"
    verbs = set(re.findall(r'(?:new Verb|Verb\.WithCount)\("(\w+)"', table.group(1)))
    assert verbs == {
        "status",
        "dump",
        "reset",
        "reload",
        "capabilities",
        "jitmap",
        "benchmark",
    }, f"the verb table lists {sorted(verbs)}"
    assert "getHelp() => UsageLine()" in body, (
        "getHelp must be built from the verb table, not a second literal"
    )
    usage = re.search(r"static string UsageLine\(\)(.*?)\n        \}", body, re.DOTALL)
    assert usage and "Verbs.Select(v => v.Usage)" in usage.group(1)
    unknown = re.search(r"if \(verb == null\)(.*?)\n            \}", body, re.DOTALL)
    assert unknown and "Verbs.Select(v => v.Name)" in unknown.group(1), (
        "the unknown-verb answer must name the verbs from the same table"
    )


def test_console_rejects_arguments_the_verb_table_does_not_define() -> None:
    # A mistyped call used to be answered by a default that looked like the
    # call the caller meant: `apm jitmap FULLL` wrote the short map, and
    # `apm benchmark 10` was silently clamped up to the 1000-iteration floor.
    # Every verb now declares what it accepts and the check runs before the
    # dispatch, so a refused call never reaches a side effect.
    body = _console_cmd_body()
    validate = re.search(r"static bool Validate\(.*?\n        \}", body, re.DOTALL)
    assert validate, "the argument check must be a helper ahead of the dispatch"
    checks = validate.group(0)
    assert "int.TryParse" in checks, "a numeric argument must be parsed, not defaulted"
    assert "BenchmarkMinIterations" in checks and "BenchmarkMaxIterations" in checks, (
        "a count outside the range Telemetry.Benchmark clamps to must be refused"
    )
    assert "OrdinalIgnoreCase" in checks, (
        "an option word must match case-insensitively, like the verb itself"
    )
    assert "takes no arguments" in checks, "a verb that takes nothing must say so"
    execute = body[body.index("public override void Execute(") :]
    assert execute.index("Validate(") < execute.index("Telemetry.Dump()"), (
        "the check must run before any verb does work"
    )
    # jitmap writes a file, so its option set is what stands between a typo and
    # a map the caller did not ask for.
    assert 'new Verb("jitmap", new[] { "full" })' in body


def test_console_verb_documentation_matches_the_dispatch() -> None:
    # bridge/README.md documents the console verbs like it documents the REST
    # endpoint: the table is the contract for anyone scripting `apm` over
    # telnet, and a verb the docs do not list is a verb nobody knows exists.
    readme = (REPO / "bridge" / "README.md").read_text(encoding="utf-8")
    assert "### Console command" in readme
    body = _console_cmd_body()
    table = re.search(r"static readonly Verb\[\] Verbs = \{(.*?)\};", body, re.DOTALL)
    assert table
    for verb in re.findall(r'(?:new Verb|Verb\.WithCount)\("(\w+)"', table.group(1)):
        assert f"| `{verb}`" in readme, f"the console docs do not list the {verb} verb"
    for needle in ("BenchmarkMinIterations", "BenchmarkMaxIterations"):
        declared = re.search(rf"const int {needle} = (\d+);", body)
        assert declared, f"{needle} must be a named bound, not a literal in the check"
        bound = declared.group(1)
        assert bound in readme, (
            f"the documented benchmark range must state the {needle} bound ({bound})"
        )


def test_config_loader_rejects_unknown_keys_instead_of_defaulting() -> None:
    # A hand-edited config with a misspelled key used to load as defaults: the
    # mod then reported the setting it was asked to change as off, with nothing
    # in the log. MissingMemberHandling.Error is what makes the typo visible.
    source = CONFIG_CS.read_text(encoding="utf-8")
    assert "MissingMemberHandling.Error" in source
    assert "load.Error" in source, "a rejected config must record why"


def test_config_load_reports_source_and_effective_values() -> None:
    # Config observability: the startup log names the file that was read and
    # the values in force after clamping, so an operator can read the active
    # config off the server log without guessing.
    source = CONFIG_CS.read_text(encoding="utf-8")
    assert "built-in defaults" in source
    assert "Config.Describe()" in source
    mod = BRIDGE_MOD_CS.read_text(encoding="utf-8")
    init = mod[mod.index("public void InitMod(") : mod.index("static Type GameType")]
    reload_body = mod[
        mod.index("public static void Reload(") : mod.index("public static void Log(")
    ]
    for name, body in (("InitMod", init), ("Reload", reload_body)):
        assert "BridgeConfigReader.Load(" in body, f"{name} must go through the config reader"
        assert "Log(load.Describe())" in body, (
            f"{name} must log the config source and the values in force"
        )


def test_example_config_documents_the_keys_it_sets() -> None:
    # The shipped example is what install_bridge.sh seeds as the live config,
    # so it must cover every key BridgeConfig declares, and it may carry
    # comments (the mod's reader accepts them, and io.load_jsonc reads the same
    # dialect on the Python side).
    fields = set(re.findall(r"public (?:bool|int|double) (\w+)\s*=", CONFIG_CS.read_text("utf-8")))
    assert fields
    from apm_suite.io import strip_json_comments

    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert "//" in text, "the example config should show the comment syntax it accepts"
    example = json.loads(strip_json_comments(text))
    assert set(example) == fields, "example config and BridgeConfig fields must match"


HOME_SH = REPO / "scripts" / "lib" / "home.sh"
DS_PATHS_SH = REPO / "scripts" / "lib" / "ds_paths.sh"


def _source_without_home(fragment: Path, command: str) -> subprocess.CompletedProcess[str]:
    """Run a source-and-print under `set -u` with HOME stripped from the env,
    the shape a bare systemd unit, cron, or `env -i` invocation has."""
    return subprocess.run(
        ["bash", "-c", f'set -u; . "$1"; {command}', "_", str(fragment)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_home_fallback_fails_loud_instead_of_aborting_on_unbound_variable() -> None:
    # Every shell entry point runs `set -u`, and a bare systemd unit, cron, or
    # `env -i` invocation carries no HOME. An unguarded $HOME there aborts with
    # "HOME: unbound variable", which names neither the cause nor the fix; the
    # fallback must fail with a message that names both. Sourcing the fragment
    # must stay side-effect free so a caller holding an explicit override never
    # pays for a home it does not need.
    assert _source_without_home(HOME_SH, ":").returncode == 0, (
        "sourcing home.sh must not fail; only taking the fallback may"
    )
    result = _source_without_home(HOME_SH, "apm_home_or_die")
    assert result.returncode != 0
    assert "HOME is unset" in result.stderr
    assert "unbound variable" not in result.stderr


def test_dedicated_dir_default_resolves_without_home() -> None:
    # ds_paths.sh is the single default every SEVENDTD_DS_DIR consumer shares,
    # and the Makefile evaluates it through $(shell) under /bin/sh. With no HOME
    # it must still resolve the path from APM_HOME rather than aborting the
    # whole make invocation on an unset-variable error.
    result = subprocess.run(
        ["sh", "-c", '. "$1" && printf "%s" "$SEVENDTD_DS_DIR"', "_", str(DS_PATHS_SH)],
        env={"PATH": "/usr/bin:/bin", "APM_HOME": "/srv/operator"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "/srv/operator/.local/share/Steam/steamapps/common/7 Days to Die Dedicated Server"
    )


def test_dedicated_dir_override_wins_over_the_home_fallback() -> None:
    # An explicit SEVENDTD_DS_DIR must resolve even with no HOME at all, or a
    # server installed outside any home directory would be unreachable.
    result = subprocess.run(
        ["bash", "-c", '. "$1" && printf "%s" "$SEVENDTD_DS_DIR"', "_", str(DS_PATHS_SH)],
        env={"PATH": "/usr/bin:/bin", "SEVENDTD_DS_DIR": "/opt/dedicated"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "/opt/dedicated"


def test_no_shell_entry_point_reads_home_unguarded() -> None:
    # The regression this guards is a bare `$HOME`/`${HOME}` in any script: it
    # aborts under `set -u` the moment a unit, cron job, or `env -i` drops the
    # variable, and it diverges from the one resolution in scripts/lib/home.sh.
    offenders = []
    for path in sorted(REPO.glob("scripts/**/*.sh")) + sorted(REPO.glob("tools/**/*.sh")):
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            if "$HOME" in code and "${HOME:-" not in code and "HOME_SH" not in code:
                offenders.append(f"{path.relative_to(REPO)}: {line.strip()}")
    assert offenders == [], f"unguarded $HOME under set -u: {offenders}"
