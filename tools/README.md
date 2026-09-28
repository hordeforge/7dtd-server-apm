# Internal backends

The supported interface is `uv run 7dtd-server-apm ...`. This directory contains its
implementation and collector backends:

- `apm_suite/` - packaged CLI, typed schemas, session audit, reports, and tests
- `apm/` - standalone collector programs (telnet scrapes, /proc samplers,
  bpftrace sources, perf wrappers) launched by the `apm_suite` capture
  orchestrator
- `host_profiler/` - Linux `perf` / bpftrace helpers, flame conversion, correlation

Backend scripts are intentionally retained because the CLI invokes them; they
are not competing public entry points. New operator workflows belong in the
CLI. Run `make check` from the repository root to format/lint every Python file,
type-check the packaged core, and execute tests.

## Text encoding

Every boundary in and out of this tree is UTF-8, including the two process
streams, which otherwise follow the locale and are ASCII under `LANG=C` (a
bare systemd unit, cron, `env -i`, `sudo` without `-E`):

| Boundary | Rule |
|---|---|
| File read | `encoding="utf-8", errors="replace"` on untrusted text (perf output, bpftrace dumps, log lines, imported bundle members); strict UTF-8 on files this tool wrote |
| File write | explicit `encoding="utf-8"`; never the platform default |
| Subprocess | `text=True` together with `encoding="utf-8", errors="replace"` |
| stdout / stderr | `apm_suite.io.force_utf8_stdio()` at the top of every `main()` and in the CLI's Typer callback; `write_stdout()` where the output is a document a caller pipes onward |
| stdin | `apm_suite.io.read_stdin_text()`, which re-pins the pipe to the same policy the file path uses (the default is the locale plus `surrogateescape`, which mangles undecodable bytes into lone surrogates) |

A survivor that is not encodable is replaced with U+FFFD at the boundary, never
carried inward. Text that becomes identity (a session or bundle name) is
normalized to NFC once, at ingestion.

Generated pages (session report, dashboard, store index, flame delta) share one
palette and type scale from `apm_suite/web_tokens.py`; the bridge WebMod panel
reuses the same literal colors. Add a page rule against a token, never a hex,
and a test (`test_every_generated_page_carries_the_shared_tokens`) fails if one
drifts.
