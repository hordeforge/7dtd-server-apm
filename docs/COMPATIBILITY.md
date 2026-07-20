# Supported compatibility matrix

**Owns:** supported game/kernel/tool versions for APM collectors.  
**Not:** how to capture ([APM](APM.md)), feature list ([FEATURES](FEATURES.md)).

What 7dtd-apm is developed and verified against. Anything outside these ranges
may work but is unsupported; `7dtd-apm doctor` reports the effective
availability of each layer on your host, and `make check-bt` compile-checks
every bpftrace probe against your installed toolchain.

| Component | Supported | Notes |
|-----------|-----------|-------|
| 7 Days to Die dedicated | V2.x, V3.x (build `7DaysToDieServe`) | Bridge hooks are verified against the game assembly hash at load; mismatches are recorded in bridge metadata and the bridge disables deep hooks. |
| Linux kernel | >= 5.15 with BTF (`CONFIG_DEBUG_INFO_BTF=y`) | Needed by bpftrace kprobes/tracepoints. Older kernels lose the `runtime_gc`, `sync_locks`, `scheduler`, and `io` layers; the session then reports them `unavailable`, never zero. |
| perf | >= 5.15 (matching kernel preferred) | `perf_event_paranoid <= 2` or root for per-process counters; frame-pointer call graphs (Mono keeps FP; dwarf cannot unwind JIT code and bloats perf.data ~100x). |
| bpftrace | >= 0.17 | `--dry-run` used by `make check-bt`. Root (via narrow `sudo -n` rules) required to attach probes. |
| Mono (game-bundled) | libmonobdwgc-2.0.so shipped with the server | GC uprobes bind to the exact .so mapped by the target process (bind mount, same inode). Managed flamegraph frames come from the bridge's `apm jitmap` export (Unity's Mono ignores `MONO_ENV_OPTIONS=--jitmap`); Burst-compiled code stays `[jit]`. Gross allocation rate (the GC-pause driver) comes from the bridge P/Invoking Boehm's native `GC_get_total_bytes` in every capture (Unity 2022 Mono lacks the managed `GC.GetTotalAllocatedBytes`); without the bridge, the opt-in `mono_alloc` probe (`--only all,alloc`) supplies it from `GC_malloc`. Allocation-site names and stop-the-world pause time come from uprobes (`mono_alloc`; `GC_stop_world`/`GC_start_world`). |
| Python | >= 3.11 | CLI and analyzers; managed with uv (`uv sync`). |
| OS | x86_64 Linux | Steam runtime layouts with spaces in paths are handled. |

## Layer -> requirement map

| Layer | Requires |
|-------|----------|
| app_sim | telnet enabled on the server, or the standalone APM bridge mod |
| cpu | perf + `perf_event_paranoid <= 2` (or root) |
| memory_cache | perf PMU counters (some events optional per CPU) |
| threads | /proc access to the target pid |
| runtime_gc | bpftrace + sudo + game Mono .so mapped |
| sync_locks / scheduler / io | bpftrace + sudo |

Missing requirements degrade the specific layer to `unavailable` in the session
manifest and withhold health grading below the coverage threshold; they never
silently pass as healthy zeros.

## Related docs

| Doc | Role |
|---|---|
| [APM](APM.md) | Validity and layer grades |
| [FEATURES](FEATURES.md) | CLI surface |
| [LOAD_PROFILE](LOAD_PROFILE.md) | Workload for compare runs |

## Changelog

- **2026-07-19:** Ownership header; related docs.
