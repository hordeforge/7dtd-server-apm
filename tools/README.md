# Internal backends

The supported interface is `uv run 7dtd-server-apm ...`. This directory contains its
implementation and collector backends:

- `apm_suite/` - packaged CLI, typed schemas, session audit, reports, and tests
- `apm/` - capture orchestration, shell collectors, analysis helpers
- `host_profiler/` - Linux `perf` / bpftrace helpers, flame conversion, correlation

Backend scripts are intentionally retained because the CLI invokes them; they
are not competing public entry points. New operator workflows belong in the
CLI. Run `make check` from the repository root to format/lint every Python file,
type-check the packaged core, and execute tests.
