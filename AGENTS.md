# AGENTS.md - 7dtd-apm

Host-only observability and performance analysis for Linux **7 Days to Die**
dedicated servers (target **V3.1.0**). Optional managed bridge times game methods.
Does **not** optimize the game or generate load.

Workspace root guide: [`../MODDING_BEST_PRACTICES.md`](../MODDING_BEST_PRACTICES.md)

## Scope

| Owns | Does not own |
|---|---|
| Process/CPU/sched/sync/runtime/memory/fs/block/net capture | Harmony performance “fixes” (use `7dtd-optimizer`) |
| Session store, audit, compare, budget, export, prune | Fake clients / protocol load (use `7dtd-loadgen`) |
| Optional `7dtd-apm-bridge.dll` instrumentation | Auto-editing EfficientServer or loadgen |
| Validity, coverage, confidence reporting | Host CCD/NUMA application (measure here; apply via systemd/`taskset`) |

## Critical rules

1. **Measure only.** Never ship optim side effects, AI LOD, or mesh budgets from this repo.
2. **Missing evidence is unavailable, never a healthy zero.** Withhold health grades below ~80% weighted coverage (see `docs/APM.md`).
3. **Baseline vs candidate must match** workload shape, collectors, duration, and server config. Prefer scenario manifests from loadgen.
4. **Passwords via env only** (`SEVENDTD_TELNET_PASSWORD`, etc.). Never put secrets in child-process argv or commit them.
5. **Python: `uv` only.** Never `pip`, `python -m pip`, or system-wide installs. Python **3.11+**, Linux required.
6. **Bridge is net48** against dedicated Managed; install under `Mods/7dtd-apm-bridge/`. EAC must be off when using server mods.
7. **Do not open a second web listener** for bridge UI; use stock WebDashboard hooks when present.
8. **No AI attribution** in commits/docs/comments. **No em dashes** in shipped text.
9. Prefer native APIs over shelling out when adding collectors; shell collectors stay under `tools/apm/` with clear ownership.

## Build / test / bridge

```bash
uv sync
uv run 7dtd-apm doctor
make check                 # ruff, shellcheck, format, mypy, pytest, bpftrace checks
make bridge-build
make bridge-install DS="/path/to/7 Days to Die Dedicated Server"
make bridge-uninstall
make clean
```

## Main CLI

```bash
uv run 7dtd-apm capture --seconds 45 --only all
uv run 7dtd-apm scenario run --seconds 60 --clients 6 --actions 500 --preset standard
uv run 7dtd-apm monitor --interval 5
uv run 7dtd-apm audit SESSION
uv run 7dtd-apm compare BASELINE CANDIDATE
uv run 7dtd-apm budget CANDIDATE --baseline BASELINE
uv run 7dtd-apm export SESSION -o support-bundle.zip
uv run 7dtd-apm prune --keep 20 --dry-run
```

Sessions default to `~/.local/share/7dtd-apm/session_*` (`SEVENDTD_APM_DIR` overrides).

### Capture presets

| Preset | Use |
|---|---|
| `standard` | Default compare runs (bridge + threads + memory + CPU sample) |
| `deep` | Broader collectors + sampled deep managed hooks |
| `forensic` | Short, high-overhead raw evidence (adds `mono_alloc`: gross-allocation churn + STW attribution - use for GC-lag diagnosis) |

## Diagnosing "laggy without CPU"

The recurring root cause is Boehm GC, not compute. Capture with `--only alloc`
(or `--preset forensic`), then read `summary.json` `metadata.lag_diagnosis`:
`profile` says spike-driven (bursty GC/stalls, low compute) vs compute-bound;
`gc.grossAllocMBPerSecond` is the churn (net `allocMBPerSecond` reads ~0 and is
misleading); `runtime_gc` layer `stw_pause_worst_ms` is the direct freeze;
`top_churn_sites`/`top_alloc_sites` name the allocators. Chunk bandwidth comes
from kernel `metadata.net.udp_send_mb_per_second` (windowed), not the bridge
`transfers` lifetime average. See README "Measured bottleneck findings".

## Layout

```text
tools/apm_suite/       CLI package (Typer entry: 7dtd-apm)
tools/apm/             Collectors, shell helpers, bpftrace sources
tools/host_profiler/   perf/bpftrace helpers and flame conversion
bridge/ApmBridge/      Optional managed timing DLL
docs/                  APM model, bridge correlation, compatibility
scripts/               bridge build/install, checks
```

## Docs map

| Path | Role |
|---|---|
| `docs/APM.md` | Capture lifecycle, validity, operations |
| `docs/APM_CS_BRIDGE.md` | Native ↔ managed correlation |
| `docs/COMPATIBILITY.md` | Game / kernel / perf / bpftrace / Mono matrix |
| `bridge/README.md` | DLL design, schema, overhead controls |
| `tools/README.md` | Backend ownership |
| `TODO.md` | Phased plan and verification log |
| `../7dtd-optimizer/docs/HOST_TUNING.md` | Host topology measured here, applied outside |

## Sibling projects

| Project | Role |
|---|---|
| `../7dtd-loadgen` | Controlled clients; APM may invoke public runner only |
| `../7dtd-optimizer` | Reviewed optim patches; never auto-mutated by APM |
| `../7days-realworld` | Terrain product; optional world under test |

Do not silently install or rewrite sibling trees.

## Stock-game research -> 7dtd-research

Anything that studies the **stock** dedicated server belongs in
[`../7dtd-research/`](../7dtd-research/), not here: reverse-engineering
narratives (`docs/`), the Mono.Cecil dump tooling (`tools/`), wire/protocol
analysis, and engine cost/loop RE. This repo owns host measurement, profiling, and budgeting;
it does not host stock-game RE docs or dumpers. When RE is needed, add it
under `../7dtd-research/` and link back. How to RE:
[`../7dtd-research/docs/re-methodology.md`](../7dtd-research/docs/re-methodology.md).
