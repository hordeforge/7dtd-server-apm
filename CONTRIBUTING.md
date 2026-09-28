# Contributing

Host-only observability and analysis for 7 Days to Die dedicated servers.
`AGENTS.md` is the authoritative rule set for this repository; this file is
the runnable path from a clean clone to a merged change. For what the tool
measures, see [`README.md`](README.md) and [`docs/APM.md`](docs/APM.md).

## Prerequisites

Linux, Python 3.11 or newer, and [`uv`](https://docs.astral.sh/uv/). Nothing
else is needed to run the host CLI or the test suite.

The full gate additionally needs `shellcheck`, `bun` (for `bunx`; the tsc,
oxlint, vnu, and shellcheck versions are pinned in
`scripts/lib/tool_versions.sh`), and a Java runtime for `vnu-jar`. Each check
names the tool it cannot find instead of failing mid-gate. `make lint-shell`
prints the installed shellcheck version and warns when it differs from the pin,
because a distro package can change findings with no commit involved; update
`SHELLCHECK_VERSION` when the new findings are the ones you want.

`bpftrace` and narrowly configured non-interactive privileges are optional and
only for `make check-bt`; GitHub Actions runners cannot validate the probes,
so that target is local-only. `dotnet` and `bun` are needed for `make
bridge-build`, and a game install is needed for `make bridge-install`.

## Setup

```bash
uv sync
uv run 7dtd-server-apm doctor
```

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
`tests/` (repo-level gates: bridge sources, packaging, CI pins); the suite takes
about a minute, so select by file or `-k` name while iterating and run everything
before opening a change.

New tests go next to the code they cover, in the file whose subject they
exercise (`tools/apm_suite/tests/test_core.py`,
`tools/apm_suite/tests/test_fuzz_parsers.py`, `tests/test_bridge_build_surface.py`,
`tests/test_dependency_surface.py`). A test drives the real entry point and
asserts the shipped result, not a re-implementation of the logic.

## Gate

```bash
make check        # lint, shellcheck, HTML, WebMod, format, mypy, tests, probes
make check-ci     # the same minus check-bt, which is exactly what CI runs
```

CI runs `make check-ci` on every push and pull request, so a green
`make check-ci` locally is the same verdict the pull request gets.

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
`bundle.js` with the source change.

## Change shape

Keep a change to one bounded slice, keep the tree green, and update the docs
the change affects in the same change. `CHANGELOG.md` is written at release
time by the maintainer; a pull request does not need to touch it.

Measurements are evidence, not opinion: a baseline and a candidate must share
workload shape, collectors, duration, and server config, and a missing
measurement is reported as missing rather than as a healthy zero.
