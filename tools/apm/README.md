# APM collector programs

This directory holds only the standalone collector programs and probe scripts
launched by the `apm_suite.capture` orchestrator: telnet scrapes, /proc
samplers, bpftrace sources, and perf wrappers. Every collector runs behind a
typed `CollectorSpec` adapter and writes a versioned `*.result.json` with exit
code, duration, tool version, sample count, and failure reason. `capture.sh`
is a thin compatibility shim that execs the CLI.

All analysis (summary scoring, health, events, managed bridge mapping,
budgets, compare, index) lives in the `apm_suite.analysis` package and runs
in-process from `apm_suite.finalize`; there are no analysis scripts here.
Missing collector artifacts are unavailable evidence, not zero values.

Use the root README and `uv run 7dtd-apm --help` for supported commands.
