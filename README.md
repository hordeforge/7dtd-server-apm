# ☣️ Geiger (7DTD Server APM)

> **Part of [HordeForge](https://github.com/hordeforge)** — High-Performance Systems Engineering for 7 Days to Die.

![CI](https://github.com/hordeforge/7dtd-server-apm/actions/workflows/ci.yml/badge.svg)
![coverage](https://raw.githubusercontent.com/hordeforge/7dtd-server-apm/badges/coverage.svg)
![license](https://img.shields.io/github/license/hordeforge/7dtd-server-apm)
![release](https://img.shields.io/github/v/release/hordeforge/7dtd-server-apm)
![languages](https://img.shields.io/github/languages/count/hordeforge/7dtd-server-apm)
![top language](https://img.shields.io/github/languages/top/hordeforge/7dtd-server-apm)

Observability and performance analysis for Linux 7 Days to Die dedicated servers. The host tool captures process, CPU, scheduler, synchronization, runtime, memory, filesystem, block, and network evidence. The optional `7dtd-server-apm-bridge` server mod adds managed game-method timing without optimization or load-generation behavior.

Project boundaries are deliberate:

- `7dtd-server-apm` measures, correlates, reports, compares, and enforces evidence gates.
- `7dtd-loadgen` creates controlled clients and workloads. APM only invokes its public runner.
- `7dtd-server-optimizer` contains reviewed runtime optimizations. APM never changes it automatically.
- Host CCD/NUMA/affinity tuning is ops, not APM or EfficientServer. Measure here, apply with systemd/`taskset`; checklist: [`../7dtd-server-optimizer/docs/HOST_TUNING.md`](../7dtd-server-optimizer/docs/HOST_TUNING.md).

## Measured bottleneck findings ("laggy without CPU")

Validated against the V3.1.0 dedicated under sibling load (20 bots + telnet
zombies). The server can miss its 20 TPS (50 ms) tick deadline while gmUpdate
compute averages ~10 ms with **~77% headroom** - so the lag is not CPU-bound.
The `forensic` preset (`scenario run --preset forensic`) attributes it:

1. **GC stop-the-world pauses are the dominant lag spike.** Boehm freezes every
   thread (including the main tick thread) for a single collection; captured
   freezes up to **321 ms** (6+ missed ticks). Driven by gross allocation
   churn, **~12.5 MB/s**, while the heap size is stable (net growth ~0). The
   older net-heap metric read ~0 and masked this entirely; gross is measured
   via the `mono_alloc` probe (Boehm `GC_malloc`), and the freeze itself via
   `GC_stop_world`/`GC_start_world` timing.
2. **Two GC drains from the same churn:** rare big STW freezes at high load,
   and constant incremental `collect_a_little` (~300/s) nibbling the main
   thread at moderate load.
3. **Allocation sources** (jitmap-resolved, ranked by bytes after the 2026-07-18
   attribution fix): the top allocator for **both** large (>=4 KB) spikes **and**
   steady small-object churn is `AstarVoxelGrid.InitScan` (A* nav-graph node
   rebuild on grid move), followed by `TerrainSubMesh.Add`, `PooledBinaryWriter.Write`
   (packet serialization), and `ItemStack.Clone`. An earlier note blamed
   `PooledBinaryWriter` *reflection* (`Type.GetMethod` per serialize); the IL shows
   an enum switch, not reflection, so that attribution was wrong and is dropped.
   The cost is allocation, not compute. See
   [`../7dtd-server-optimizer/docs/measured-scaling.md`](../7dtd-server-optimizer/docs/measured-scaling.md) §4b.
4. **Main-thread-bound.** The 20 TPS game loop is single-threaded, so any
   pause or per-entity cost lands on one thread across ~200.
5. **Per-entity tick cost is linear** at ~0.08 ms/entity/tick, so 1000 AI
   entities = ~80 ms/tick before AI/path/net - the scale wall.
6. **Chunk cost is CPU/allocation, not network bandwidth.** Even with bots
   roaming across the map (validated: continuous position reconciliation + real
   run speed let bots roam thousands of metres), kernel UDP send stays low
   (~0.2-1.8 MB/s) because chunks compress well - while the server spends
   ~10 MB/s of allocation serializing them. The lever is the serialization
   allocation, not the wire. (The bridge's ~60 MB/s `transfers` figure is a
   since-reset average inflated by the join world-download.)

Lever order: cut gross allocation at source (throttle the A* graph updates +
pool `AstarVoxelGrid.InitScan` node buffers, network serialize-once, guard the
dedicated `GC.Collect`), then entity-tick striding for scale. Full evidence:
[`../7dtd-server-optimizer/docs/OPTIMIZATION_CANDIDATES.md`](../7dtd-server-optimizer/docs/OPTIMIZATION_CANDIDATES.md)
§4b/§4c.

## Quick start

```bash
uv sync
uv run 7dtd-server-apm doctor
make bridge-build
make bridge-install DS="/path/to/7 Days to Die Dedicated Server"
uv run 7dtd-server-apm capture --seconds 45

# Capture while the sibling project generates six joined clients.
uv run 7dtd-server-apm scenario run --seconds 60 --clients 6 --actions 500 --preset standard
```

Passwords should be supplied through `SEVENDTD_TELNET_PASSWORD`; they are never
placed in child-process arguments. EAC must be disabled when using server mods.

## Harness integration (7dtd-loadgen comparison runs)

`7dtd-loadgen`'s stock-vs-zdtd harness (scripts/compare_sut.sh) drives this
tool automatically on the stock phase of every comparison scenario:
`COMPARE_APM=1` runs `7dtd-server-apm capture --seconds N --no-app` over the
connected window, finalizes the session under `stock/apm/session_*/`, and
summarizes it into the comparison surface (layer scores, IPC, GC alloc rate,
lag verdict). The bridge must be installed in the stock dedicated server (the
`make bridge-install` step above). `COMPARE_APM=0` disables; see
`../7dtd-loadgen/docs/SUT_COMPARE.md` for the full picture.

## Capture presets

| Preset | Purpose | Expected observer overhead |
|---|---|---|
| `standard` | Bridge, threads, memory counters, CPU sampling | Low to moderate |
| `deep` | All collectors and sampled deep managed hooks | Moderate |
| `forensic` | All available raw evidence for short investigations | Highest |

`--preset` is a `scenario run` option; it selects the `capture --only` collector
set (`standard` = `app,threads,memory,cpu`, `deep` = `all`, `forensic` =
`all,alloc`). A bare `capture` takes `--only` directly, not `--preset`.

`scenario run` drives the sibling load generator: `--bot-mode` selects the
behaviour (including `demolition` terrain destruction), `--spawn-entity` /
`--spawn-per-player` / `--spawn-every-ms` scale telnet-spawned zombies,
vehicles, or turrets into the hundreds, `--warmup` delays the capture window
until load is steady, and `--reset-bridge` (default on) zeroes bridge counters
so managed section totals cover exactly the capture window. The analyzer emits
a per-subsystem `attribution` block in `csharp_bridge.json` (deep-sampled
sections scaled by the bridge sample rate) answering "where does the frame
budget go", plus an approximate exclusive frame-core time and a path-pipeline
balance (FindPath enqueues vs drains vs computes) for path-admission
experiments. `--rally` teleports the whole cohort into one cluster after
warmup (pair with the loadgen `bait` mode to isolate AI/combat cost from
chunk streaming). `scenario matrix PLAN.json` runs a labeled experiment
sequence, running a cleanup console command between experiments (`killall`
by default; it removes spawned entities but does not reset the save world -
use the loadgen `reset_world.sh` between terrain-mutating runs); a reusable
plan ships at `plans/campaign.default.json`. `doctor` also flags a stale installed bridge
DLL and disabled DeepMode; the audit warns when frame spikes occurred during
the capture window. Attribution deltas appear in `compare` output when both
sessions used `--reset-bridge`.

Stall analysis ("laggy without CPU"): a server can miss its 20 TPS deadline
while barely using CPU. The bridge counts late ticks (>60 ms) and cumulative
overage per window (`app_sim.late_ticks` is the lag headline). The off-CPU
probe tracks the main thread and splits blocked time by state, but note that
most main-thread off-CPU time is the *healthy* frame-pacing sleep (the server
sleeps between ticks when under budget), so the scheduler layer scores only
D-state (disk) blocks; `SLOW_VFS_MAIN` marks main-thread file IO.
The analyzer's `stall_correlation` attributes each worst frame spike to nearby
evidence and, by duration match, to Mono stop-the-world GC pauses
(`cause: gc_pause`) - the most common "laggy without CPU" culprit. The bridge
also reports window-scoped GC pressure (`metadata.gc`: managed allocation
MB/s and full-collection count); since Mono's Boehm GC is non-generational,
allocation rate is the pressure gauge and each collection it triggers is a
frame hitch. For root-causing WHICH code allocates, `scenario run --preset
forensic` (or `capture --only all,alloc`) runs a bounded Boehm-allocator probe
that stacks large allocations; finalize annotates those stacks with the
managed symbol names (via the bridge jitmap), naming the allocating methods. The report also flags main-thread-bound
servers: the thread sampler reports the main thread's CPU% and its share of
total process CPU, so a server pinned on one thread (while the rest of a
many-core box sits idle) scores on the cpu layer even though box-wide load
looks low. All of this rolls up into a single `lag_diagnosis` verdict
(printed at the end of `capture` and shown atop the dashboard) that ranks the
causes that fired - e.g. "laggy (49 late ticks, 2510 ms overage) - gc_pauses;
main_thread_bound; lock_contention" - so a run answers "why is it laggy" in
one line. The verdict is also exported to Prometheus
(`sevendtd_apm_lag_cause_severity{cause=...}`, alertable) and shown per-session
in the index for triage across many captures. The bridge
counts late ticks (>60 ms) and cumulative tick overage per window, and the
analyzer's `stall_correlation` block pairs each worst frame spike with the
events that happened within +/-2 s (rendered on the dashboard). The load
generator reports client-perceived latency (LiteNetLib RTT p50/p95/max and
spike counts) in `loadgen_stats.json`, separating wire lag from sim stall.

Flamegraph symbolization: Unity's embedded Mono ignores
`MONO_ENV_OPTIONS=--jitmap`, so the bridge exports the perf map itself
(`apm jitmap [full]`, sent by every `scenario run` capture and by bare
`capture --symbolize`; the latter defaults OFF because the
JIT burst runs on the server's main thread and can stall a loaded server,
so pass it only for bench/flamegraph runs). The map
lives in the mod's disk-backed telemetry directory; only a symlink is placed
at the tmpfs path perf hardcodes (`/tmp/perf-<pid>.map`). Recording uses
frame-pointer unwinding (measured ~40x more resolvable frames than dwarf on
Mono, at ~1% of the perf.data size); unresolved anonymous-exec frames are
labeled `[jit]` (mostly Burst-compiled code and Mono trampolines) and
unknown-symbol frames keep their module (`[libmonobdwgc-2.0.so]`).

Use identical workload manifests, collector selection, and durations for
baseline/candidate comparisons. Missing evidence is reported as unavailable;
it is never scored as a healthy zero. A health grade is withheld below 80%
weighted coverage.

## Main commands

```bash
uv run 7dtd-server-apm doctor
uv run 7dtd-server-apm capture --seconds 45 --only all --reset-bridge
uv run 7dtd-server-apm finalize SESSION
uv run 7dtd-server-apm audit SESSION
uv run 7dtd-server-apm index
uv run 7dtd-server-apm scenario run --preset deep --warmup 90 --label exp1
uv run 7dtd-server-apm scenario run --preset deep --bot-mode demolition --no-spawn
uv run 7dtd-server-apm scenario run --preset deep --spawn-entity vehicleTruck4x4 --spawn-per-player 5
uv run 7dtd-server-apm scenario run --preset deep --bot-mode bait --rally --label combat
uv run 7dtd-server-apm scenario matrix plans/campaign.default.json
uv run 7dtd-server-apm monitor --interval 5
uv run 7dtd-server-apm compare BASELINE CANDIDATE
uv run 7dtd-server-apm budget CANDIDATE --baseline BASELINE
uv run 7dtd-server-apm export SESSION -o support-bundle.zip
uv run 7dtd-server-apm prune --keep 20 --dry-run
uv run 7dtd-server-apm flame build SESSION_DIR
uv run 7dtd-server-apm bridge --help
make check     # full local gate incl. bpftrace probe validation
make check-ci  # CI variant (no bpftrace; GitHub Actions has no host kernel)
```

Sessions live under `~/.local/share/7dtd-server-apm/session_*` (override with
`SEVENDTD_APM_DIR`). Each contains raw artifacts,
structured summaries, collector states, coverage/confidence, a workload
manifest when run as a scenario, offline HTML, and an integrity manifest.

## Environment variables

All configuration is environment-based; there are no config files on the Python
side. `uv run 7dtd-server-apm doctor` reports the resolved values (the telnet secret
appears only as set/unset). An exported-but-empty variable is treated as unset.

| Variable | Default | Valid values | Purpose |
|---|---|---|---|
| `SEVENDTD_TELNET_PASSWORD` | unset | server telnet password | Secret for the app-layer scrape and telnet actions. Supply via the environment or Typer's `envvar` wiring, never as argv. Required for the `app` collector to authenticate. |
| `SEVENDTD_APM_DIR` | `~/.local/share/7dtd-server-apm` | writable directory | Session store root (`session_*`, `.scenario`, `.trash`). |
| `SEVENDTD_DS_DIR` | Steam default dedicated-server path | existing directory | Dedicated install used by `doctor`, bridge build/install scripts, and probe helpers. |
| `SEVENDTD_GAME_DIR` | Steam default client path | existing directory | Client install fallback for `make bridge-build` when the dedicated Managed assemblies are absent. Build-time only; no runtime code reads it. |
| `APM_KEEP_SESSIONS` | `40` | integer >= 0 | Newest sessions kept by post-capture auto-prune; `0` (or any value <= 0) disables auto-prune. Non-integers warn and fall back to `40`. |
| `APM_PRUNE_GRACE_HOURS` | `24` | float >= 0 | Soft-delete window in `<store>/.trash/`; `0` hard-deletes immediately. Non-numeric values warn and fall back to `24`. |
| `SEVENDTD_LIVE` | unset | `1` enables | Test gate only (`pytest`): opts into live-server tests that need a running dedicated server. Never read at runtime. |

## Repository map

- [`bridge/README.md`](bridge/README.md) - DLL design, installation, schema, and overhead controls
- [`docs/APM.md`](docs/APM.md) - capture lifecycle, validity rules, and operations
- [`docs/APM_CS_BRIDGE.md`](docs/APM_CS_BRIDGE.md) - native-to-managed correlation
- [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) - supported 7DTD/kernel/perf/bpftrace/Mono matrix
- [`docs/LOAD_PROFILE.md`](docs/LOAD_PROFILE.md) - canonical heavy load profile and the tier/scale ladders
- [`docs/FEATURES.md`](docs/FEATURES.md) - supported capabilities and project boundaries
- [`docs/ROADMAP.md`](docs/ROADMAP.md) - prioritized improvement backlog
- [`tools/README.md`](tools/README.md) - private backend ownership
- [`TODO.md`](TODO.md) - phased implementation and verification log

Python 3.11+, `uv`, and Linux are required. `make check` additionally shells
out to `shellcheck`, `node`/`npx` (the tsc/oxlint/vnu versions are pinned in
`scripts/lint-*.sh`), and `java` (vnu-jar); each check names its missing tool
instead of failing mid-gate. `make help` lists every target with its
prerequisites. `perf`, bpftrace, and narrowly configured non-interactive
privileges are optional; `doctor` reports exactly which layers can run. Game
and kernel compatibility is recorded per session.
