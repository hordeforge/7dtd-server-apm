# Contributing

Host-only observability and analysis for 7 Days to Die dedicated servers.
`AGENTS.md` is the authoritative rule set for this repository; this file is
the runnable path from a clean clone to a merged change. For what the tool
measures, see [`README.md`](README.md) and [`docs/APM.md`](docs/APM.md).

## Prerequisites

Linux, [`uv`](https://docs.astral.sh/uv/), and the Python the repo pins in
`.python-version` (currently 3.12; `uv` provisions it, no system interpreter
needed). `pyproject.toml` still accepts `>=3.11`, so the pin is what a local
run and a CI run both resolve; nothing else is needed to run the host CLI or
the test suite.

The full gate additionally needs `shellcheck`, `bun` 1.4.2 (the CI pin; for `bunx`; the tsc,
oxlint, vnu, and shellcheck versions are pinned in
`scripts/lib/tool_versions.sh`), and a Java runtime for `vnu-jar`. Each check
names the tool it cannot find instead of failing mid-gate. `make lint-shell`
prints the installed shellcheck version and warns when it differs from the pin,
because a distro package can change findings with no commit involved; update
`SHELLCHECK_VERSION` when the new findings are the ones you want.

`bpftrace` and narrowly configured non-interactive privileges are optional and
only for `make check-bt`; GitHub Actions runners cannot validate the probes,
so that target is local-only. `dotnet` and `bun` are needed for `make
bridge-build`, and a game install is needed for `make bridge-install`. The
.NET SDK version is pinned in `global.json` and the bridge build refuses to
compile with a dotnet that does not satisfy that pin, because the muxer only
warns on a mismatch; install the pinned SDK, or change the pin on purpose.

## Setup

```bash
uv sync --locked
uv run 7dtd-server-apm doctor
```

`--locked` is the same flag the Makefile targets and CI use: a plain `uv sync`
re-resolves and rewrites `uv.lock` when `pyproject.toml` has drifted, which
hides the drift the gate is supposed to report. Dependency changes go through
`uv lock` on purpose.

`doctor` reports the resolved paths, which layers can run, and any missing
credential. It is the first command to run on a new machine, and the fastest
way to see whether a later capture will have full coverage.

## Edit-test loop

```bash
uv run --locked pytest tools/apm_suite/tests/test_core.py -k test_list_sessions
```

`uv run` is enough: it syncs the project environment on demand. The `Makefile`
targets add `--locked --project .` and a repo-local UV cache, so prefer them
for the gates. Tests live in `tools/apm_suite/tests/` (package behavior) and in
`tests/` (repo-level gates: bridge sources, packaging, CI pins); the full suite takes a
minute or two, so select by file or `-k` name while iterating and run
everything before opening a change.

New tests go next to the code they cover, in the file whose subject they
exercise (`tools/apm_suite/tests/test_core.py`,
`tools/apm_suite/tests/test_fuzz_parsers.py`, `tests/test_bridge_build_surface.py`,
`tests/test_dependency_surface.py`, `tests/test_packaging_surface.py`). A test
drives the real entry point and asserts the shipped result, not a
re-implementation of the logic.

## Gate

```bash
make check        # lint, shellcheck, HTML, WebMod, format, mypy, tests, probes
make check-ci     # the same minus check-bt, which is exactly what CI runs
```

CI runs `make check-ci` on every push and pull request, so a green
`make check-ci` locally is the same verdict the pull request gets. `make
coverage` is the one CI target with no local gate behind it: it re-runs the
suite under coverage and writes the `.coverage` data the README badge is
rendered from.

If a version needs bumping, `scripts/check_version.py` (wired into `make test`)
enforces the two independent version pairs: `pyproject.toml` and
`tools/apm_suite/__init__.py` for the host CLI, and `bridge/ApmBridge/ModInfo.xml`,
the `Version` const in `bridge/ApmBridge/BridgeMod.cs`, and the mod version
claim in `bridge/README.md` for the bridge. Git tags mirror the bridge version
only.

A host CLI bump also needs `uv lock`: the lock records the project version, and
the `Makefile` targets run `uv run --locked`, so a bump without it fails the
gate on the lockfile rather than on the version. The gate checks the lock too.

A `.ts` edit under `bridge/ApmBridge/WebMod/` must be recompiled, or the
`lint-webui` freshness step fails: run `make bridge-build` and commit
`bundle.js` with the source change. The same tests cap the shipped
`bundle.js` and `styling.css`; the dashboard downloads both on every page, so
raise those budgets only with a measurement.

`make package` writes the release zip, its `.sha256`, and a `.buildinfo.txt`
recording the SDK, the TypeScript version, and the sha256 of the game
assemblies the DLL was compiled against. Two builds of one commit agree only
when those inputs match, so keep the record with the artifact.

## Change shape

Keep a change to one bounded slice, keep the tree green, and update the docs
the change affects in the same change. `CHANGELOG.md` is written at release
time by the maintainer; a pull request does not need to touch it.

Measurements are evidence, not opinion: a baseline and a candidate must share
workload shape, collectors, duration, and server config, and a missing
measurement is reported as missing rather than as a healthy zero.
