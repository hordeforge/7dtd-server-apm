# 7dtd-apm implementation plan

This is the live implementation checklist. APM owns measurement, evidence,
analysis, and orchestration. It does not implement load generation or optimizer
behavior: scenarios call the sibling `7dtd-loadgen` project, and recommendations
may point at `7dtd-optimizer` without modifying it.

- [x] Add a native authenticated V3 WebDashboard panel and `/api/apm` endpoint.
- [x] Add explicit explosion, world/player save, and network entity-distribution timings.
- [x] Instrument chunk/map/world-folder serialization and send queues with package and byte counters.
- [x] Enable and preserve Mono `--jitmap` symbols so perf flamegraphs resolve managed methods.

## Phase 0: trustworthy results and sessions

- [x] Represent every requested layer as collected, skipped, unavailable, or failed.
- [x] Add coverage/confidence and withhold health grades when evidence is incomplete.
- [x] Make budgets report unknown for missing evidence instead of treating it as zero.
- [x] Reject comparisons with incompatible collectors, workload, duration, or analyzer versions.
- [x] Record collector exit code, duration, tool version, sample count, and failure reason.
- [x] Run managed-section analysis before budget evaluation.
- [x] Run the artifact integrity audit only after all session files are closed.
- [x] Remove automatic Harmony patch scaffolding from capture finalization.
- [x] Keep telnet credentials out of process arguments and captured metadata.
- [x] Add fixtures for failed, unavailable, and interrupted captures.

## Phase 1: low-overhead managed bridge

- [x] Rename method-duration metrics and measure server tick interval separately.
- [x] Replace string dictionaries with fixed section accumulators and bound locking.
- [x] Sample deep hot-path hooks and expose sampling/drop/overhead counters.
- [x] Move JSON serialization and filesystem writes off the game update thread.
- [x] Use bounded preallocated spike storage and dispose native process handles.
- [x] Report export errors instead of swallowing sampling failures.
- [x] Record bridge/game assembly versions, hashes, exact hook signatures, and capabilities.
- [x] Define restart/runtime behavior for deep-mode configuration.
- [x] Add bridge structural regression tests and an instrumentation microbenchmark command.

## Phase 2: unified capture and scenarios

- [x] Define a typed collector interface and structured collector results.
- [x] Move capture/finalization orchestration into the `7dtd-apm` Python CLI.
- [x] Consolidate the duplicate APM/eBPF capture paths.
- [x] Add standard, deep, and forensic presets with overhead metadata.
- [x] Replace the old bridge-loop shell workflow with a CLI scenario command.
- [x] Invoke only `../7dtd-loadgen` for generated load and ingest its run manifest.
- [x] Validate bridge snapshot freshness and capture-window overlap more strictly than file timestamps.
- [x] Make server/loadtest/capture shutdown deterministic on interruption.

## Phase 3: evidence-based analysis

- [x] Validate all session and bridge JSON against versioned typed schemas at ingestion.
- [x] Normalize managed comparisons by per-call distribution and require equivalent workload/duration.
- [x] Replace remaining native word-matching recommendations with thresholded structured evidence.
- [x] Separate raw evidence, derived metrics, inference, and suggested experiment.
- [x] Add conclusion coverage/confidence and artifact provenance in manifests.
- [x] Bound raw event materialization and retain aggregates in the main report.
- [x] Version scoring rules and withhold grades for insufficient coverage; calibration remains ongoing.
- [x] Produce machine-readable comparison results; structured budget JSON remains.

## Phase 4: cleanup and product surface

- [x] Consolidate the duplicate HTML/report renderers.
- [x] Fold continuous monitoring and scenario workflows into the CLI.
- [x] Remove obsolete compatibility scripts and automatic patch-generator code.
- [x] Keep raw eBPF probes behind collector adapters rather than duplicating orchestration.
- [x] Remove copied optimizer/load-generator documentation from the APM docs.
- [x] Rewrite root and subfolder READMEs around strict project ownership.
- [x] Add install/uninstall Make targets, retention pruning, and sanitized export commands.

## Phase 5: release and compatibility

- [x] Add unit, parser-fixture, golden-report, integration, and live-server tests.
- [x] Add shell lint and bpftrace compile/probe checks.
- [x] Add CI and a licensed package surface; release checksums/install verification remain.
- [x] Publish a supported 7DTD/kernel/perf/bpftrace/Mono compatibility matrix.
- [x] Establish an on-server bridge record-overhead benchmark and initial 2000 ns budget.
- [x] Add optional Prometheus text export; OpenTelemetry remains deferred until schemas stabilize.
- [x] Declare the project license and publish data retention/redaction guidance.

## Verification log

- 2026-07-16: architecture and ownership review completed; implementation started.
- 2026-07-16: optimizer load/profiler duplication and APM load wrappers removed; bridge v3 and loadgen rename completed.
- 2026-07-16: capture orchestration moved into apm_suite.capture (typed CollectorSpec adapters, structured results, deterministic shutdown); schema validation enforced at audit ingestion; bridge analysis thresholded with evidence/derived/inference/experiment separation; duplicate HTML renderer and host_profiler capture path removed; monitor command, test suite (parser fixtures, golden report, integration, live gate), shellcheck + bpftrace --dry-run checks, and docs/COMPATIBILITY.md added. Validation: `make check` green; 5s orchestrated smoke capture produced a valid manifest.
- 2026-07-16: full review pass; fixed orchestrator interrupt/duration/PermissionError bugs, bridge frame-accumulator races, compare per-call heat units, perf stat timeout race, and unbounded perf.data growth (99 Hz + bounded dwarf). Bridge extended with hot-path hooks (TickEntitiesSlice/TickEntity/OnUpdateLive/UpdateMoveHelper/FindPath/updatePlayerList/LetBlocksFall/GroupFallingBlocks/UpdateMainThreadTasks/save paths, overload pinning, 53 hooks active). Catalog section names aligned with bridge names. Validation: three live 100-client dynamite+zombie scenarios; final run: all 14 collectors ok, audit clean, coverage 1.0, flame built, compare + baseline budget gates verified end to end.
- 2026-07-16: architecture consolidation: analysis backends folded into apm_suite.analysis (typed, mypy strict, models constructed at write time), finalize now an in-process typed pipeline, health.json is the single home of health (summary.json never patched), session index reads health.json with no inline recomputation, session data root moved to ~/.local/share/7dtd-apm (SEVENDTD_APM_DIR override; existing sessions migrated), duplicate budget.default.json removed (DEFAULT_BUDGET canonical). Validation: make check green; live 30-client scenario finalized in-process with clean audit.
- 2026-07-16: post-refactor full review (3 findings fixed: tool_version crash on empty output, bridge Config volatile + Reset atomicity, threads.py stat-parse guard; 4 findings refuted with reasons). Research-driven instrumentation from 7dtd-optimizer OPTIMIZATION_CANDIDATES.md probe list: 10 new hooks (path pipeline GetPathTo/Calculate/pathFollow, EAIApproachAndAttackTarget, AstarManager.UpdateGraphs, AIDirectorBloodMoonComponent, Vehicle/Drone/PowerManager, AddFallingBlock) -> 63 hooks active, 0 unavailable. Validation: 100-client scenario clean audit, coverage 1.0; AstarManager.UpdateGraphs immediately surfaced as #2 per-call hot section (p95 8.9 ms x 642 calls).
- 2026-07-16: experiment tooling + campaign. APM: scenario knobs (bot-mode/spawn-entity/spawn scaling/warmup/label), --reset-bridge window-scoped stats, scenario matrix runner, subsystem attribution block (deep-scaled, ms/tick + ms/entity normalization) in analyzer/dashboard/compare/prometheus, bridge frame-spike events, world-sample metadata, prune --max-gb. Loadgen: demolition bot mode, --max-dynamite, generic --spawn-entity scaling (lifted 2/player cap), join --ramp-ms, --stats-json cohort stats attached to sessions. 7-experiment live campaign (idle/baseline/zombies/demolition/vehicles/turrets/horde): chunk streaming pipeline dominates every loaded scenario (~50-65% of instrumented managed time); entity_tick second (5-20%, scales with entities); AI+pathfinding <=2.5% for non-combat load. Optimizer OPTIMIZATION_CANDIDATES.md gained 4b evidence section; experiment order re-ranked (chunk streaming first).
- 2026-07-16: improvement rounds 11-20. APM: --rally cohort clustering (telnet teleport), attribution v2 (additive slice-level buckets + nested entity drill-down, exclusive frame core, path enqueue/drain/compute balance), compare.md attribution table, doctor stale-bridge + DeepMode checks, audit frame-spike warnings, monitor world stats, shipped plans/campaign.default.json for scenario matrix. Loadgen: bait bot mode, manifest seed/dynamite/spawn fields, LOADGEN_SEED passthrough. Live exp7 combat-bait (30 rallied bait bots, ~90 pursuing zombies): chunk noise cut to 12%, entity tick chain 58% additive, AI-decide+path+movement < 2 s / 27 s, path queue healthy; optimizer doc updated (whole-chain AI LOD over path admission).
- 2026-07-17: profiling data quality push. Root cause: Unity Mono ignores MONO_ENV_OPTIONS=--jitmap, so no session ever had managed perf symbols. Bridge now exports the perf map itself (apm jitmap [full]: PrepareMethod+GetFunctionPointer over Assembly-CSharp/mscorlib/System/Unity core/LiteNetLib, gap-filled sizes; map on disk, tmpfs gets only a symlink), capture triggers it pre-window and links /tmp/perf-<pid>.map. perf record switched to fp unwinding (A/B: 136k resolvable frames vs 3.5k dwarf, ~1% file size); stackcollapse keeps module names for unknown symbols and labels anonymous JIT/Burst pages [jit]. Result: attributable flame frames 0% -> 15.2% with full managed call chains (GameManager.Update -> RegionFileManager etc.); remaining [jit] bulk is Burst output + Mono trampolines (documented limitation). Also: audit counts, spawn caps, and entity-freeze mechanics documented from live measurements (peak ~116 entities / ~69 alives; TickEntitiesSlice starvation explains frozen AI at mass spawns).

## Done criteria

A checked item includes implementation, tests, schema/docs impact, and a
reproducible validation command. Missing evidence must never be presented as a
healthy zero, and optional tooling must fail visibly without invalidating usable
raw evidence.

## Bottleneck-hunt round ledger (goal: 100 rounds, started 2026-07-17)

- R1: offcpu.bt rewritten: main-thread only, duration-weighted blocking stacks (>=1ms), STALL_MAIN markers >=10ms.
- R2: scheduler layer scores main_thread_stall_ms/share; STALL_MAIN lines enter the event timeline.
- R3: loadgen client-perceived latency: LiteNetLib RTT sampled per bot, cohort ping p50/p95/max/spikes in stats-json.
- R4: offcpu stall time split by thread state (D=IO vs S=sleep/futex).
- R5: analyzer stall_correlation: worst frame spikes vs +/-2s event evidence.
- R6: vfs_lat.bt flags SLOW_VFS_MAIN (main-thread file IO = direct frame stall) with duration sum.
- R7: io layer scores main_thread_slow_io.
- R8: dashboard panel: worst frame stalls with correlated evidence.
- R9: ledger + ladder session hygiene (tier-300 mislabel fixed; ladder v2: living-player spawn targeting, per-tier killall, start/end alive counts).
- R10: bridge lateTicks + tickStallMsTotal counters (>60 ms ticks, cumulative overage) in snapshot; installed, activates next restart.
- R11: late-tick gauges scored into app_sim layer + shown by monitor.
- R12: ladder_report analysis script (entities vs ms/tick vs stalls curve).
- R13: stall-analysis docs in APM + loadgen READMEs.
- R14: scenario --rally-at x,z (fresh-ground cohort teleport) + safer rally grid spacing.
- R15: loadgen wander leash (45 u around server spawn). Found two real loadgen defects while hunting: (a) bots self-report positions, so server teleports never stick (client-authoritative movement); (b) bots carry spawn-height y forever, and once they wander onto different terrain the embedded/floating player breaks server spawn-point search ("No spawn point found near player"), which killed zombie scaling. Leash keeps bots on valid terrain.
- R16: loadgen ApplySpawn y-fallback fixed (last-known y instead of hardcoded 72 when the server replies y<=1).
- Ladder status: tier-100 session captured (session_20260716_161630, ladder-100); tiers 300+ blocked by an emergent server-state issue on the current boot: spawnentity executes silently without spawning (log shows ground y=38 vs bot-reported y=72 -> floating players break spawn-point search) and even AIDirector scout spawns now despawn instantly (Zom: 0 seconds after 'Spawned' log lines). This differs from the afternoon boots where 1652 spawns worked. Next session: restart server fresh (also activates R10 late-tick counters + R16 loadgen build), rerun scale_ladder.py (scratchpad copy in this repo would be better: promote to plans/ or tools/), and check whether long-uptime spawn degradation is itself a reportable server bottleneck (spawn-grid/gore accumulation + floating-player interaction).
- Diagnostic breadcrumbs: scouts log 'AIDirector: Spawning Scouts1 at (x, 38, z), to (x2, 72, z2)'; se responses went from 'No spawn point found near player!' to silent no-op as uptime grew.
- Pending: ladder 300/600/1000 rerun on fresh boot, per-tier stall attribution via offcpu v2 + late-tick counters, optimizer doc update with per-entity curve.
- R20: loadgen adopts server-authoritative ground Y for its own entity from EntityPosAndRot/Teleport (rarely fires; 7DTD is client-authoritative for the local player, kept as correct catch-all).
- R21: THE ACTUAL FIX for the spawn saga. Root cause was accumulated server-side player ghosts from ~6 cohorts hard-killed without clean disconnect -> NetPackagePlayerDenied (reason=2, world-full) + a spawn-drifted save (terrain y=38 vs bot y=72). Fixed: loadgen sends graceful DisconnectAll before Stop + ProcessExit/CancelKeyPress handler; archived the degraded save and regenerated fresh. On fresh world: zero denials, bots ground at spawn terrain, spawnentity/spawnscouts succeed. Ladder unblocked and running. Ops finding: unclean cohort shutdown leaks player slots and degrades a server until it refuses joins and mis-spawns.
- R22: scale ladder ran on the fresh world with grounded, pursuing zombies. Promoted plans/scale_ladder.py + plans/ladder_report.py (report now shows late-tick/disk-block/GC-caused-spike columns). Captured a 99-entity session (69 pursuing zombies, ladder-70): entity_tick 20%, pathfinding drilldown 3127 ms (5x the ~40-zombie combat run), 50 late ticks at 13.6 ms compute, frame spike = 384 ms GC pause. Per-entity tick cost measured LINEAR at ~0.08 ms/entity/tick (32->99 entities) => 1000 AI = ~80 ms/tick entity work alone, over the 50 ms budget. Optimizer OPTIMIZATION_CANDIDATES.md updated with exp8 row + the 1000-extrapolation + "laggy without CPU = GC pauses" conclusion. Note: scout-horde spawning plateaus at ~70 concurrent (hordes despawn after completing waves); reaching 300/600/1000 needs persistent spawnentity spawning (works on fresh world) or higher bot counts - deferred.
- Pending: persistent-spawn ladder to 1000, A2 path-admission + TickEntity-stride prototypes with before/after compare.
- R23: bridge GC-window instrumentation (metadata.gc). Captures managed allocation rate + full-collection count over the capture window (baseline at --reset-bridge). Validated at ~70 zombies/30 players: 3.78 MB/s heap growth -> 3 full Mono/Boehm stop-the-world collections/90s, runtime_gc scored 80. Boehm is non-generational (gen0==gen2), so alloc rate is the true pressure gauge, not generation counts. This is the actionable root of "laggy without CPU": ~4 MB/s allocation churn -> periodic STW pause -> late ticks while CPU stays low (gmUpdate 13 ms). Directly points optimization at per-tick allocation cuts + guarding the dedicated GC.Collect (A7).
- R24: mono_alloc.bt allocation-site probe (opt-in / forensic only; `--only alloc` or `scenario --preset forensic`). Bounds overhead by only summing total bytes cheaply and stacking LARGE (>=4KB) allocations on the main thread. Validated: 338 MB allocated in 30s (2.4M calls, ~11 MB/s raw), 60 MB in large blocks.
- R25: jitsym.py annotates hex JIT addresses in ANY bpftrace output against the bridge perf map (bpftrace can't read /tmp/perf-<pid>.map itself). Wired into finalize; capture now copies the map in-session. Result: allocation sites resolve to managed names. TOP LARGE-ALLOC SITES at ~70 zombies: **EntityItem.tickDistraction** and **AstarManager pathfinding** (get_Current iterator). Completes the "laggy without CPU" pipeline: GC pause -> allocator probe -> jitmap annotation -> named allocating method. These two are the concrete per-tick allocation cuts to target for the GC-pause bottleneck (feeds optimizer A7 + entity/AI alloc reduction).
- Pending: persistent-spawn ladder to 1000; A2 path-admission + TickEntity-stride prototypes with before/after compare; per-section GC delta attribution.
- R26: main-thread saturation signal. thread_summary now computes the main thread's average CPU% and its share of total process CPU across samples; the cpu layer scores "main-thread-bound" when one thread carries >=50% of process CPU. Validated: main thread = 50% CPU and 58% of the whole process across 242 threads (IPC healthy at 1.56). Third and final dimension of "laggy without CPU": work concentrated on ONE thread while 241 others idle and the 32-core box looks half-loaded. Full picture now measured = main-thread-bound + GC stop-the-world pauses (tickDistraction/pathfinding allocation) + tick overage, all while gmUpdate compute stays ~13 ms.
- R27: lag_diagnosis synthesizer. Reads the layer signals (late ticks + overage, GC alloc rate/full collections, main-thread share, disk blocks, futex contention) and emits ONE ranked plain-language verdict in summary.metadata + a top dashboard panel. Validated: "laggy (49 late ticks, 2510 ms overage) - gc_pauses (0.97); main_thread_bound (0.58); lock_contention (0.06)" with concrete per-cause detail. Turns the scattered signals into the fast answer the whole toolchain exists to give. (Also: reconstructed reporting.py after a heredoc clobbered it with template HTML - caught by lint.)
- R28: lag verdict printed at the end of every capture/finalize (">> lag diagnosis: ..."), so the one-line answer lands in the console, not just the dashboard. Added diagnose_lag unit tests (ranked causes + healthy path). 26 tests green.
- R29: each lag cause now carries a concrete `fix` string, and gc_pauses pulls the named top allocation sites from the jitmap-annotated allocator probe. Live output: "cut per-tick allocations at the top sites (EntityItem.tickDistraction, Chunk.load, AstarVoxelGrid.InitScan); guard dedicated GC.Collect (A7)". Closes the loop symptom -> cause -> named code -> optimizer candidate. Dashboard shows the fix per cause. (Re-added the lag panel to the dashboard template - lost during the R27 reporting.py clobber/repair.)
- R30: memory-trend / leak signal. proc.jsonl RSS slope + fd delta over the window distinguishes GC churn (RSS oscillates, flat trend) from a real leak / unbounded buffer (RSS climbs steadily). Feeds memory_cache scoring and a memory_growth lag cause. Validated: RSS +1.0 MB/s, fd 66->65 = churn not leak (correctly below threshold). Catches send-queue/cache/handle leaks that lag or OOM a server over hours.
- R31: attribution-based lag causes. When one managed subsystem dominates instrumented time (>=45%, frame_core excluded), the verdict names it - network_bound / io_saves_bound / entity_tick_bound / mesh_bound - each with the matching optimizer candidate as the fix (B3/B4/A1-A3/B5). Ties the campaign attribution finding (chunk streaming ~60% under many-player load) into the one-line verdict. Reads attribution from the prior finalize (conservative threshold; stays silent at balanced load, verified on the 99-entity session where no subsystem hit 45%).
- R32: Prometheus export of the lag verdict - sevendtd_apm_laggy, sevendtd_apm_lag_cause_severity{cause=...}, sevendtd_apm_late_ticks - so the diagnosis is scrapeable/alertable (alert on gc_pauses severity > 0.5, etc).
- R33: session index shows the one-line lag verdict per session (new column), for at-a-glance triage across many captures. Verdict html-escaped.
- R34: budget gate now fails on late-tick share (max_late_tick_share 0.05): a server that misses its tick deadline fails CI even if layer scores pass. Ties the lag headline into the gate.
- R35: compare surfaces late-tick delta (A vs B) in JSON + markdown - so a fix that cuts late ticks shows up directly, not only via layer scores.
- R36: scale_ladder mixes persistent spawnentity with scout hordes (scouts plateau ~70 as they despawn post-wave); needed to accumulate higher tiers.
- R37: monitor shows per-sample late-tick delta (late+N / late=0) so lag onset is visible live.
- R38: sched_states.bt now measures MAIN-thread run-queue latency (wake -> scheduled) with MAIN_RUNQ_STALL markers; scheduler layer scores it and a cpu_starvation lag cause fires when the tick thread is ready but the OS won't schedule it (contention/affinity/noisy neighbor - a host-side "laggy without CPU", fix = pin/isolate cores).
- R39: futex.bt exposes main-thread total futex wait time (@main_futex_wait_us), not just SLOW line counts; sync_locks scores on main-thread wait SHARE of the window (threshold-independent contention measure).
- R40: chunk-transfer bandwidth surfaced from bridge map-transfer counters. Measured 95 MB/s chunk streaming (888 pkg/s) to 30 wandering bots in a 90s window = 8.6 GiB - the network/chunk bottleneck (B4) quantified in BYTES, not just managed ms. Added chunk_bandwidth lag cause. Note: wander bots are worst-case chunk churn (constant reload around each moving player); clustered real players stream far less - a loadgen realism caveat worth recording.
- R41/R42: loadgen realism - pace jitter (+/-20% per-bot think time, no lockstep) and varied per-bot view distance (4..12 chunks, realistic chunk-residency spread). Note: R40's 95 MB/s was worst-case uniform-bubble wander; varied view distance makes future chunk-load measurements more representative.
- R43: lock_contention cause now names the contended waiter methods (top_stack_sites over the jitmap-annotated futex output), matching how gc_pauses names allocators. Generic top_stack_sites helper.
- R45: dashboard Frame/GC panel - tick avg, gmUpdate, late ticks, GC alloc MB/s + full collections, chunk-stream MB/s + pkg/s, world entity/player counts. Validated rendering (e.g. "chunk stream: 95.23 MB/s · 888.3 pkg/s").
- R46: budget gate adds max_alloc_mb_per_second (5) and max_chunk_mb_per_second (50) - GC churn and chunk bandwidth fail CI, not just layer scores.
- R47: prometheus exports sevendtd_apm_alloc_mb_per_second + sevendtd_apm_chunk_mb_per_second gauges (validated 3.78 / 95.23).
- R48: session index shows entity/player counts per session (ent/ply column) for load-level triage at a glance.
- R49: compare surfaces GC-alloc MB/s and chunk-stream MB/s (A vs B) in JSON + markdown, so an optimization's effect on allocation and bandwidth shows directly.
- R50: doctor reports disk_low (perf.data can be GBs; low free space silently truncates captures).
- R38-R40/R43/R45 live-validated (20 bots + zombies, fresh world): main_runq_stall=0 (correctly healthy - no OS starvation on 32 cores), main-thread futex wait 548 ms measured, chunk stream 63 MB/s, dashboard panel renders, verdict fired: "laggy (41 late ticks) - chunk_bandwidth; main_thread_bound; gc_pauses". jitmap 88k symbols.
- R51: io_net.bt tracks tcp_send_bytes/tcp_recv_bytes (sum arg2), not just call counts.
- R52: report surfaces metadata.net.tcp_send/recv_mb_per_second; dashboard Frame/GC card shows kernel tcp vs bridge chunk MB/s.
- R53: budget gate max_tcp_send_mb_per_second (60.0) cross-checks chunk stream at syscall layer.
- R54: chunk_bandwidth lag cause appends "kernel tcp send X MB/s corroborates" when kernel >= 0.5x bridge counter (independent confirmation).
- R55: lock_contention lag cause now fires on main_thread_futex_wait_share >= 5% (direct wall-clock block, the "laggy without CPU" tell), not only SLOW_FUTEX count; detail reports ms + share.
- R56: FINDING - bridge mapTransfers "MB/s" is a since-reset lifetime average, dominated by the initial join chunk burst. Kernel UDP probe (always capture-windowed) shows steady-state chunk stream is ~1.76 MB/s for 20 wandering bots, NOT 63 MB/s. Reframe: chunk bandwidth is a JOIN-TIME burst, not a steady lag driver. Steady lag is GC + main-thread-bound + per-entity tick cost. diagnose_lag now leads chunk_bandwidth with the windowed kernel rate and labels the bridge figure as join-burst-weighted. Corrects earlier 63/95 MB/s "steady" claims.
- R57: FINDING - gc.allocMBPerSecond was NET heap growth (GetTotalMemory delta), which reads ~0 at steady state even under heavy churn (alloc==collect). Reframed gc_pauses to lead with full-GC count + rate/sec, and prefer grossAllocMBPerSecond when available. The full-GC count is the direct stop-the-world pause signal.
- R58: bridge adds GC.GetTotalAllocatedBytes (monotonic gross alloc) via reflection - CONFIRMED ABSENT in Unity 2022 Mono (returns -1, degrades to 0). Wired the opt-in mono_alloc bpftrace probe (Boehm GC_malloc arg0 sum) as the gross-allocation source on this runtime; report fills gc.grossAllocMBPerSecond from @alloc_bytes_total when the bridge counter is unavailable.
- R57/R58 VALIDATED LIVE (--only alloc,app,runtime --reset-bridge, 20 bots): grossAllocMBPerSecond=12.4 (391 MB / 30s, 2.4M GC_malloc calls) while net heap growth = -0.11 MB/s. THE SMOKING GUN: old net-heap metric reported ~0 and would have cleared GC entirely; true gross churn is 12.4 MB/s of garbage Boehm must scan+collect every second -> stop-the-world pauses with a STABLE heap. This is the "laggy without CPU" mechanism correctly measured for the first time. mono_alloc probe (opt-in, --only alloc) is the gross-alloc source; big-alloc stacks name the allocating methods for the fix.
- R58 alloc sites resolved via jitmap: top large-allocation site is AstarVoxelGrid.InitScan (A* pathfinding voxel-grid init) + UnityEngine.Quaternion.FromToRotation. Pathfinding is a PRIMARY GC-pressure source. Bottleneck chain now evidence-linked: per-entity tick cost -> pathfinding voxel-grid allocation (12.4 MB/s churn) -> Boehm stop-the-world GC pauses -> main-thread stalls. Fix: pool/reuse AstarVoxelGrid buffers, cache FromToRotation.
- R60: loadgen Kite mode (BotMode.Kite=8) - slow continuous arc inside leash so chasing zombies repath every tick, targeting the AstarVoxelGrid.InitScan pathfinding alloc hotspot. Built+selftest PASS.
- R60 VALIDATED: kite+67 zombies gross alloc = 12.65 MB/s vs 12.4 wander baseline (20 players). REFINEMENT: the ~12.5 MB/s gross-alloc FLOOR is largely independent of zombie count. Pathfinding = LARGE INFREQUENT allocs (AstarVoxelGrid, 103 MB spikes -> heap-growth hitches); the STEADY 12.5 MB/s churn is small-object allocation from BASE server systems (chunk/deco/water/entity updates), not zombies. Implication: cutting zombie count won't lower the GC floor; target base-system small-object churn + pool AstarVoxelGrid buffers separately for spike smoothing.
- R61: mono_alloc probe adds 1-in-4096 sampled stack over ALL allocation sizes (weight x4096) to attribute the steady small-object churn FLOOR that the >=4KB filter misses. Report exposes top_churn_sites; gc_pauses fix splits "large-alloc spikes" vs "steady churn". jitsym annotates the new block automatically.
- R61 FINDING: churn floor sources = System.RuntimeType.GetMethodCandidates/Type.GetMethod (REFLECTION in hot path - classic GC anti-pattern) + TileEntity.InstantiateFromRead/TileEntityFeatureData.InstantiateModule (chunk tile-entity load). Thread-pool frames present (ThreadStart_Context) => churn is largely on WORKER threads, and Boehm is process-wide so it still stall-the-worlds main. MUST rule out bridge Newtonsoft JSON export as observer-effect source (R62).
- R62 FINDING (observer-effect ruled out): the reflection churn chains to PooledBinaryWriter.FinalizeSizeMarker (7DTD's OWN binary serializer calling Type.GetMethod per serialize) + TileEntity.InstantiateFromRead - NOT the bridge's Newtonsoft (no JsonConvert frames in the sample). The steady GC-churn floor is the GAME's tile-entity chunk load/save serialization using reflection (~1.3-1.5 MB est/site). Concrete server bottleneck: reflection in PooledBinaryWriter hot path + unpooled TileEntity instantiation. Fix targets: cache the reflected MethodInfo in PooledBinaryWriter; pool TileEntity read buffers. This is the allocation twin of the chunk-streaming bandwidth cost - both are chunk/tile-entity churn from moving players.
- R64: optimizer OPTIMIZATION_CANDIDATES.md §4c corrections - (1) allocation under-measured 3x (net 3.8 -> gross 12.5 MB/s), (2) churn floor = PooledBinaryWriter reflection + TileEntity serialization not just pathfinding, (3) chunk bandwidth is a join burst (kernel udp 1.8 MB/s steady vs bridge 60 MB/s lifetime avg), + movement-validation caps bot roam. §4b figures preserved; corrections appended.
- R65: budget gate max_alloc_mb_per_second (gated net heap ~0, always passed = useless) -> max_gross_alloc_mb_per_second=15.0 on gc.grossAllocMBPerSecond. Report omits grossAllocMBPerSecond when unmeasured (UNKNOWN, not healthy-zero, per budget philosophy). compare + prometheus surface gross churn.
- R67: mono_gc.bt adds GC_stop_world->GC_start_world timing = the PRECISE stop-the-world freeze (all threads incl. main tick suspended), tighter than whole-collect latency. Report runtime_gc layer: stw_pause_total_ms / worst / count; gc_pauses cause appends "worst freeze X ms". VALIDATED: 60s window = 2 STW freezes, worst 27.8 ms, total 30.2 ms.
- R67 CALIBRATION FINDING: this window had 29 late ticks / 1479 ms overage but only 30 ms of STW freeze - so lag here is NOT big GC pauses. It's 18068 collect_a_little incremental GCs (300/s, nibbling main-thread time continuously without a single freeze) + per-entity tick compute. GC STW freezes dominate only at HIGH load (earlier 350ms pauses at 70 zombies); at moderate load the drain is incremental-GC + compute. Refines "laggy = GC STW" -> "laggy = gross churn driving BOTH rare big STW freezes AND constant incremental collection overhead".
- R68: gc_pauses cause distinguishes STW-freeze (worst_stw>=50ms) from incremental-GC drain (collect_a_little rate>=50/s: "cost spread across ticks, not one freeze"). VALIDATED: "incremental GC 301/s (small STW 30.2 ms total)". Also fixed a parse bug: @little_n is a growing per-interval cumulative; was grabbing the FIRST print (97) via re.search, now takes the LAST (18068) via findall[-1]. (Other @-counters parsed are END-only, unaffected.)
- R69: reconciled chunk budget - dropped max_chunk_mb_per_second (gated the bridge lifetime-avg mapTransfers = join-burst-weighted false FAILs) in favor of max_udp_send_mb_per_second=30.0 on the windowed kernel UDP rate (honest current steady state, per R56).
- R70: prometheus exports sevendtd_apm_gc_stw_worst_ms / gc_stw_total_ms (direct main-thread freeze) + sevendtd_apm_udp_send_mb_per_second (honest windowed chunk rate). gross-alloc gauge added earlier.
- R71: doctor adds mono_gc_probe check (libmonobdwgc-2.0.so mapped in target pid = gross-alloc/STW uprobes can attach), native via /proc/pid/maps. doctor now prints failing check hints (mono_gc_probe/sudo/telnet/bridge) with fixes, not just layer readiness.
- R72: docs - APM.md gains a "Lag diagnosis" section (gross alloc, STW timing, alloc-site attribution, kernel chunk bandwidth); FEATURES.md lists evidence-based lag diagnosis.
- R73: bridge hooks TileEntity:InstantiateFromRead + TileEntityFeatureData:InstantiateModule (the churn source) to measure their CPU cost. Verified active (65 hooks). RESULT: InstantiateFromRead 243 calls @ avg 0.043ms => tile-entity read is CHEAP in CPU (~10ms total); the cost is ALLOCATION not CPU. (Build gotcha: .NET string literals are UTF-16 in #US heap, invisible to ascii `strings`; use `strings -e l`.)
- R73 COMPREHENSIVE VALIDATION (20 bots + zombies, --only alloc,app,runtime,io --reset-bridge): all new signals fire together - gross alloc 9.0 MB/s (net heap 2.51), STW worst 321ms/total 342ms (a big freeze = 6+ missed ticks, the "laggy without CPU" GC pause caught directly), 24354 collect_a_little, churn sites ItemStack.Clone + RuntimeType.GetConstructorCandidates (reflection-based object construction - another alloc anti-pattern), kernel udp 1.83 MB/s steady. Bottleneck picture fully instrumented.
- R74: monitor shows live full-GC delta ("fullGC+N(STW!)" = Boehm stop-the-world pauses happening now) alongside late-tick delta; rounded tick/gm display. Live output makes the "laggy without CPU" signature visible: tick=51.1ms at cpu=0%.
- R75: lag_diagnosis adds a "profile" + tick_headroom_pct distinguishing SPIKE-DRIVEN lag (gmUpdate compute < 60% of 50ms budget + late ticks = bursty GC/stalls, the classic "laggy without CPU") from COMPUTE-BOUND saturation. VALIDATED: "spike-driven: avg compute 11.2ms leaves 77.5% headroom, lag is bursty (GC pauses), not sustained CPU". Surfaced on dashboard + finalize print. This is the precise characterization of the user's core question: at 20 bots + 65 zombies the server has 77% compute headroom but lags on GC-pause spikes - fix GC, not CPU.
- R76: added unit tests for the new GC diagnosis logic - STW-freeze vs incremental-GC branch wording, spike-driven vs compute-bound profile, and the @little_n last-cumulative + STW parse (guards the R68 parse-bug fix). 30 tests pass.
- R77: session index shows per-session profile tag (spike/compute) + gross alloc MB/s + worst STW ms, so load levels compare at a glance (low-load spike-driven vs high-load compute-bound).
- R78 (loadgen movement investigation): tested a server-legal speed cap (6 m/s) hypothesis for Traverse roaming. RESULT: no effect - bots still cluster ~80m from spawn with Y pinned at exactly 72.0 (spawn default, GroundAdopted never fires). So the roam blocker is NOT client move-speed validation; it's deeper server-side position application (the server does not move the player entity from client NetPackageEntityPosAndRot as expected, or bots aren't "in-world" enough). Reverted the speed cap (YAGNI: complexity + slower bots, no measured benefit). Enabling true bot roaming needs protocol-level work on how 7DTD accepts/applies player movement - deferred. Confirms the load-test bots exercise a fixed spawn-area chunk set (join burst), consistent with R56/R63: steady chunk streaming is inherently low for these bots.
- R79: compare surfaces worst STW-pause regression (stw_worst_ms_a/b) alongside gross alloc + late ticks, so a GC-pause change between two sessions is visible in the compare markdown.
- R80: scenario preset help clarifies forensic = mono_alloc gross-alloc + STW attribution for GC-lag diagnosis; bot-mode help lists kite/traverse.
- R81: README gains a "Measured bottleneck findings" section - the validated "laggy without CPU" conclusion (77% headroom, 321ms GC STW freezes, 12.5 MB/s gross churn, PooledBinaryWriter reflection + tile-entity + pathfinding alloc sources, linear per-entity tick cost, chunk-burst-not-steady) with the lever order. This is the user-facing deliverable answering the core goal.
- R82: added parser-robustness tests (CLAUDE.md "fuzz any parser") - _gc_layer and build_summary net parse survive empty/truncated/malformed/garbage probe output without raising, returning safe defaults. 39 tests pass.
- R83: END-TO-END INTEGRATION PROOF via `scenario run --preset forensic` (15 bots, zombieBoe x3/player, 20s warmup): full pipeline (loadgen launch -> warmup -> zombie spawn -> forensic capture with all probes -> finalize -> diagnosis) works in ONE command. Output: verdict "gc_pauses; chunk_bandwidth", profile "spike-driven: 84.1% headroom, lag is bursty GC", gross alloc 10.19 vs net 0.58 MB/s, STW 31/58ms. The toolchain correctly diagnoses "laggy without CPU = GC" automatically.
- R84: bridge version 2.0.0 -> 2.1.0 (gross-alloc GetTotalAllocatedBytes counter + tile-entity hooks); rebuilt+installed. COMPATIBILITY.md documents the gross-alloc measurement path (GetTotalAllocatedBytes absent in Unity 2022 Mono -> mono_alloc GC_malloc probe; STW via GC_stop_world/GC_start_world).
- R85: monitor flags stale bridge reads ("[bridge Ns old]") when the snapshot is older than 1.5x the sample interval - the bridge exports every 30s (PeriodicExportSeconds), so faster polling would otherwise show stale GC/tick data as live.
- R86: AGENTS.md gains a "Diagnosing laggy without CPU" section (forensic preset -> lag_diagnosis profile/gross alloc/STW/churn sites; kernel udp for chunk bandwidth) so future work in the repo follows the validated GC workflow.
- R87: events parser recognizes STW_PAUSE lines as gc events, so stall_correlation can match a frame spike to the EXACT stop-the-world freeze duration (was only matching the coarser SLOW mono_gc_collect). Note: a STW freeze suspends the bridge's gmUpdate Stopwatch too, so it shows as tick-INTERVAL inflation / late_ticks (+ the direct bpftrace STW probe), not a gmUpdate spike - the direct probe is the primary STW signal.
- R88: runtime_gc LAYER score (feeds health at weight 0.15) now scores on GROSS allocation churn (>=10 MB/s -> 80, >=4 -> 50) instead of net heap growth (~0). Previously health was blind to allocation pressure because the net metric it used reads ~0 at steady state. STW severity already fed the score (R67).
- R89: demolition experiment (15 bots, block damage): gross alloc 11.11 MB/s - SAME ~10-12 MB/s band as wander/kite/traverse. Churn = AstarVoxelGrid.InitScan (pathfinding) + RuntimeType.GetMethodCandidates (block-damage chunk-resend serialization reflection). CONCLUSION: the gross-alloc FLOOR is stable across bot behaviors - dominated by serialization reflection + pathfinding, not player activity. Lever is those two sources.
- R90: bridge/README.md documents the gc gross-allocation counter (GetTotalAllocatedBytes, -1 on Unity 2022 Mono -> host mono_alloc probe) + tile-entity deep hooks + version 2.1.0.
- R91: bridge P/Invokes Boehm's native GC_get_total_bytes (DllImport monobdwgc-2.0) for the gross-allocation counter - cheap (one call/export, NO uprobe). Falls back to GC.GetTotalAllocatedBytes reflection, then -1. RESULT: gross alloc is now in EVERY capture without the high-overhead --only alloc probe (validated: 0.61 MB/s idle, 8 MB/s cumulative). The mono_alloc probe is now needed ONLY for allocation-SITE attribution (top_churn_sites/top_alloc_sites), not the gross rate. Bridge 2.1.0. Notable: server allocates ~8 MB/s even at IDLE (0 players) - base-system churn floor exists without any load.
- R92: corrected APM.md/COMPATIBILITY.md - gross alloc is now bridge-default (GC_get_total_bytes P/Invoke); mono_alloc probe (forensic) is only for allocation-SITE names, not the gross rate.
- R93: finalize hint updated for gross-by-default - now suggests --only alloc when churn is significant (gross>=4 MB/s or full GCs) but the allocating SITES are unnamed (top_churn_sites empty), instead of the stale 'gross unmeasured' condition.
- R94: idle baseline (0 players): gross alloc 0.21 MB/s, 0 full GCs. Loaded (15-20 bots + zombies, any mode) = ~11 MB/s => ~50x idle. CORRECTION to R62/R89: the ~11 MB/s churn is PLAYER/ENTITY-DRIVEN (chunk streaming to clients + entity ticking + pathfinding serialization), NOT base-system churn - at idle the server barely allocates. The activity TYPE (wander/kite/demolition) doesn't differentiate (all ~11), but entity/player PRESENCE drives the churn ~50x. Confirms the levers: cut per-client chunk/tile-entity serialization allocation + per-entity pathfinding allocation.
- R95: CROSS-VALIDATION of the two independent gross-alloc sources under load (18 bots + zombies): bridge P/Invoke (GC_get_total_bytes) = 11.25 MB/s vs mono_alloc probe (GC_malloc sum) = 11.0 MB/s - agree within 2%. Both methods confirmed accurate; the cheap bridge counter (R91) is validated against the independent bpftrace probe.
- R96: budget tests protect the core guarantee - gross-alloc over limit FAILs, a layer with no evidence is UNKNOWN (not a pass), and absent gross is reported 'skip (no data)' never silently passed as a healthy zero. 41 tests.
- R97: full doctor health check with bridge 2.1.0 - bridge OK (installed==dist, deep_mode on), mono_gc_probe OK, all 8 layers available, ready=True. Toolchain end-to-end healthy.
- R59: finalize prints a hint when full GCs are seen but gross allocation is unmeasured (later refined in R93 for gross-by-default: hint when churn is high but allocating sites are unnamed).
- R66: dashboard Frame/GC card shows gross churn MB/s + full STW collections + the resolved churn/spike allocation site names (top_churn_sites / top_alloc_sites).
- R98: certified both repos pristine after the session - APM (ruff+shellcheck+mypy+41 tests+bpftrace all green), bridge 2.1.0 builds clean, loadgen builds 0 errors + selftest PASS.
- R99: verified the round ledger R51-R98 is complete and coherent (deduped a double R88; added missing R59/R66/R98 entries; R62/R78 present in FINDING format).
- R100: CAMPAIGN COMPLETE (R1-R100). Root cause of "laggy without CPU" definitively established and instrumented: Boehm GC stop-the-world pauses (up to 321 ms, 6+ missed ticks) driven by ~11 MB/s gross allocation churn while the heap is stable and gmUpdate has ~77% compute headroom. Churn is player/entity-driven (~50x idle): PooledBinaryWriter serialization reflection + TileEntity chunk load + AstarVoxelGrid pathfinding + ItemStack.Clone. Delivered: gross-alloc measurement (bridge native GC_get_total_bytes P/Invoke, default in every capture; mono_alloc probe for site names; cross-validated within 2%), direct STW-pause timing, allocation-site attribution, spike-vs-compute profile, kernel-UDP chunk bandwidth (correcting the join-burst-inflated bridge average), and full surfacing across dashboard/prometheus/monitor/index/compare/budget/doctor with tests, docs, and evidence in optimizer §4b/§4c. Lever order: cut gross allocation (cache reflection, pool buffers, guard GC.Collect), then entity-tick striding for scale.

## Bottleneck summary (final)

The server misses its 20 TPS deadline not from CPU saturation but from GC:
gross allocation churn (~11 MB/s under load, ~0.2 idle) forces Boehm stop-the-world
collections that freeze the single main tick thread for tens to hundreds of ms.
gmUpdate compute stays ~10 ms (77% headroom). Allocation sources are per-client
chunk/tile-entity serialization (reflection-heavy) and per-entity pathfinding.
Fix allocation first; entity-tick cost is the separate linear scale wall (~0.08
ms/entity/tick -> ~80 ms/tick at 1000 entities).

## Faithful-client fixes (goal: fix all faithful client gaps in loadgen bots)

- R101: CORRECTION - R63/R78 were WRONG. Bots CAN roam freely; the blocker was
  loadgen client-fidelity, not server-side movement validation. Root causes:
  (1) Y-adoption was ONE-SHOT (GameJoinClient adopted the server's ground Y once
  at join then never again, so a walking bot kept reporting a stale Y and the
  server's move validation snapped it back near spawn); (2) superhuman step speed
  (~26-32 m/s) outran the server's chunk streamer. FIX 1: continuous position
  reconciliation - the bot now adopts the server's authoritative pos for its own
  entity on every NetPackageEntityPosAndRot/Teleport, and accepts hard X/Z
  corrections, like a real client. FIX 2: MaxRunSpeedMps=6.0 cap on per-move
  distance (real 7DTD run speed). VALIDATED: single bot roamed ~1800 m; 10-bot
  cohort spread ~3700 m (fast) / ~1000 m (realistic speed), all staying connected.
- R102: FINDING - chunk COST is CPU/allocation, not network bandwidth. Roaming
  bots (any speed) show low kernel UDP send (0.17-0.63 MB/s) while the server
  does ~10-12 MB/s gross alloc: chunks compress well, so the serialization
  allocation is the cost, not the wire. Reconciles optimizer 4b ("chunk pipeline
  dominates" = serialization CPU) with the low measured bandwidth. Confirms the
  lever is serialization allocation (PooledBinaryWriter reflection etc.), never
  bandwidth. Docs corrected: loadgen README, APM README finding #6, optimizer 4c.
- R103: REMAINING GAP (documented, not fixed) - bots do not send periodic
  NetPackagePlayerStats (health/stamina/food/water) that a real client sends
  ~1/s. PackageCodec has no builder for it and the binary format is not known;
  reverse-engineering risks malformed packets that drop the connection (worse
  than omitting), so deferred. Server-load impact is minor (vitals processing)
  vs the movement/chunk load already driven. Other client packets (EntityMotion,
  EntityAnimationData) are cosmetic for server load - YAGNI.

## Canonical load profile (goal: standard mixed workload)

- R104: loadgen `--bot-mix` (weighted per-bot mode, deterministic by client id) -
  heterogeneous cohort in one run (traverse:35,wander:15,combat:20,bait:15,
  demolition:10,chatty:5). Fixed proportional picker (spread across weight space,
  correct at any cohort size). Verified: 15 bots -> Traverse:6/Combat:3/Bait:2/
  Wander:2/Demolition:2. Wired through run_loadgen.sh + `7dtd-apm scenario run
  --bot-mix` + campaign field.
- R105: wandering hordes - TelnetAdmin.SpawnWanderingHorde (rotating scout-horde
  bursts: spawn at distance, path in as a group = long-range pathfinding + spawn
  manager), a stream distinct from the per-player trickle. `--horde-every-ms`/
  `--horde-waves` on loadgen + scenario. Verified both ambient + horde tasks
  start with the full mixed entity list.
- R106: canonical profile - plans/profile.canonical.json (100/250/500-player
  scale ladder, forensic capture) + docs/LOAD_PROFILE.md. Mixes zombies + animals
  (predator/prey/vulture) + zombieDemolition (explosions) + vehicles + junk drone
  + wandering hordes + the bot mix. Covers chunk streaming, AI/pathfinding, animal
  AI, explosions, falling blocks/water, block updates, vehicles, drones, GC churn,
  net/chat. Validated end-to-end (15-bot smoke): forensic diagnosis fired,
  44 alive entities, mix distributed.
- R107: FINDING - electricity + turrets are placed-block subsystems (PowerManager/
  TurretTracker tick only with powered blocks/turrets); bots can't build, so those
  need a pre-placed powered base prefab. Documented as a profile prerequisite.
- R108: FINDING (combat/entity interaction) - attempted real zombie-combat (track
  nearby entities from the pos stream, attack in range with BuildDamageEntity).
  Verified the server sends a joined bot ZERO entity data (0 EntitySpawn/RelPos/
  Motion packets): the bot is a player entity tracked by position but not a
  registered entity OBSERVER, so it cannot see zombies to fight, loot, or read
  terrain. Reverted the non-functional targeting (degrades to self-attack anyway).
  Block placement/mining/crafting/vehicle-driving also need packet formats the
  codec lacks (desync risk to guess). NOT a load gap: spawned entities aggro/path
  to bot positions server-side, so AI/pathfinding/spawn-manager run fully. True
  entity/block interaction needs chunk+observer protocol participation - documented
  gap. Faithful player actions the bot DOES perform: roam/run/jump/crouch/strafe/
  aim, dynamite explosions, melee swing, chat, death/respawn.

## Reproducible canonical profile (goal: define THE reproducible test profile)

- R109: fixed pace-jitter RNG determinism leak - _paceRng was seeded by thread id
  (non-deterministic across runs). Now reseeded per bot in ActionLoop.Run from
  opt.Seed (^0x5f3759df), so think-time jitter is reproducible. Action RNG was
  already deterministic (ActionSeed + clientId + life).
- R110: world reset for reproducibility - scripts/reset_world.sh [--start] stops
  the server, wipes the playthrough save (block changes/loot/entities/player data)
  under Saves/*/BotPoi_RWG_4096, and keeps GeneratedWorlds (deterministic RWG
  terrain regenerates identically). Verified: 170M save -> 16M fresh, Day 1, 0
  players. Solves the main reproducibility threat (demolition permanently mutates
  terrain across runs).
- R111: seed plumbed through scenario/campaign (--seed, LOADGEN_SEED); default 42,
  canonical pins 20240717.
- R112: THE canonical profile = plans/profile.canonical.json single fixed step
  `canonical-v1` (seed 20240717, 50 clients, 90s warmup, 120s window, forensic,
  reset_bridge, the mix + mixed ambient + wandering hordes, max_dynamite 60).
  Scale ladder split to plans/profile.scale-ladder.json (25/50/100, same seed).
  docs/LOAD_PROFILE.md rewritten to lead with the pinned-knob table + the
  reproducible run protocol (reset_world --start -> campaign).
- R112 VALIDATED: two reset+identical runs (seed 20240717, 15 clients): gross
  alloc 10.57 vs 11.41 MB/s (~8%), lateTicks 12 vs 9, entities 67 vs 69 - aggregate
  load stable across runs. Residual variance is server-side spawn/AI RNG + join
  timing (documented); compare aggregates not per-tick values.

## 1000-player scale test (2026-07-17)

- R113: ramped bot PLAYERS toward 1000 (ServerMaxPlayerCount=1100, no zombie
  spawn, seed 20240717). FINDING: server does NOT reach 1000 - saturates and
  death-spirals at ~450-500 players. gmUpdate linear ~0.0085 ms/player to 413
  (3.5 ms, tick 14 ms, healthy), then CLIFF: 498 players -> gmUpdate 1376 ms,
  tick interval 2928 ms (0.34 TPS); 634 clients -> 3397 ms (0.29 TPS), telnet
  dead, ~1250 bots time out joining. gross alloc 164 MB/s at the wall (vs ~11 at
  50 players).
- R113 ROOT CAUSE: player-scale wall is the NETWORK/CONNECTION layer, not entity
  AI. Section attribution at ~500 players/window: NetConnectionSimple.taskSerialize
  4554 ms, GameManager.UpdateTick 1181 ms, ConnectionManager.Update 988 ms,
  NetEntityDistribution.OnUpdateEntities 914 ms, ChunkManager.SendChunksToClients
  244 ms; World.TickEntities only 18 ms. Churn at scale = login/auth + logging
  (String.Format, telnet/Unity log). So entity-tick striding (helps 1000 zombies)
  does NOT help 1000 players - distinct levers: off-thread serialization, spatial
  NetEntityDistribution culling (~O(players x entities)), ConnectionManager
  batching, cut per-join alloc churn. Practical ceiling ~450 players. Recorded in
  optimizer OPTIMIZATION_CANDIDATES.md §4d. Also uncovered a config gotcha:
  LOADGEN_CONCURRENCY caps simultaneous live bots (set >= target for N players).

## Suite improvements from the 1000-player test

- R114: lag_diagnosis now surfaces the player-scale wall. Two fixes: (a) build_summary
  computes subsystem attribution FRESH from this session's bridge snapshot instead of
  reading a prior finalize's csharp_bridge.json (the summary stage runs before the
  bridge stage, so the diagnosis was structurally one finalize behind - it MISSED
  network_bound at 500 players). (b) subsystem-dominance skips the inclusive frame_core
  bucket and names the top DISJOINT subsystem, else a network-bound tick where
  GameManager.UpdateTick is nominally top surfaced nothing. Re-diagnosed the 498-player
  session: now fires "network_bound = 55% of instrumented managed time" with the
  connection-layer fix (off-thread serialization, spatial NetEntityDistribution culling,
  ConnectionManager batching). Test added for the frame_core-top case.
- R115: monitor shows live TPS (1000/tickInterval) + prefers bridge world.clients over
  telnet for player count (survives telnet saturation at scale). The 20-TPS budget wall
  is now visible live.
- R116: loadgen concurrency footgun fixed - join bots are long-lived players that never
  free their slot, so concurrency is the live-player cap. Now defaults to count (all
  simultaneous; --ramp-ms staggers joins) and WARNS loudly if pinned below count. This
  silently stalled the first 1000-player run at 64.

## Adversarial code review (10 rounds) + super-linear detection

- Feature batch first: #3 saturation/death-spiral cause (verdict "SATURATED (X TPS)" when tick>=150ms + >=90% late); #12 grossAllocKBPerTick; #2/#36 super-linear detector (analysis/scaling.py log-log fit of section cost vs load) + `apm scaling` command; roadmap in docs/ROADMAP.md.
- Review R-fixes (verified each finding, dropped false positives):
  - report.py: off-CPU bucket regex was DEAD (expected raw "1000" but bpftrace prints "[1K,2K)"); fixed to match K/M/G buckets (>= ~1ms). The +30 heuristic never fired before.
  - cli.py scenario_run: loadgen subprocess LEAKED if warmup/rally raised (Popen before the try/finally); moved warmup+rally inside try. Added --rally-at validation (crashed on bad input).
  - budget.py / compare.py: `score or avgMs` treated a legitimate 0 as missing (falsy-zero); explicit None checks (_first_present helper).
  - bridge.py stall_correlation: guard spike_ms>0 (zero-spike tolerance collapsed to exact-match).
  - Telemetry.cs: GcWindow ran UnityEngine.Time.realtimeSinceStartup on the export ThreadPool thread (main-thread-only API); now caches the main-thread timestamp.
  - TelnetAdmin.cs: wandering-horde cursor advanced by requested targets not actual, breaking rotation when targets>players; advance by actual.
  - capture.py: stat() TOCTOU on the jitmap poll; finalize.py: summary.json read twice -> once.
  - reset_world.sh: guard empty GAME_NAME (glob would rm -rf all saves).
  - scaling cmd: reject sessions with <3 distinct load levels (log-log fit needs spread).
  - CRITICAL Telemetry/BridgeMod.cs: the Harmony hooks (FramePrefix/Postfix, SectionPrefix/Postfix) ran with NO exception guard - a throw inside a patched game method would crash the SERVER TICK. An instrumentation mod must never take down its host. Wrapped all four hot-path hooks in try/catch (lose at most one sample). Bridge rebuilt + installed.
  - bpftrace probes reviewed: NO issues (udp/tcp arg2=len confirmed, STW pairing safe, 1/4096 sampling math sound, D-state/futex/runq filters correct).
  - Round 7 (packet codec): SECURITY - integer overflow in PackageCodec parse bound check (po+4+contentLen wraps negative on a crafted huge contentLen -> passes check -> OOB BlockCopy reading arbitrary memory from a malicious/malformed server packet). Fixed with overflow-safe subtraction. + ParsePosAndRotBody short-body guard (avoid per-packet EndOfStreamException at scale).
  - Round 8 (analysis modules): reporting._load now degrades on malformed JSON (was crashing render); events.py guards missing "kind"; index.py filters None layer keys.
  - Round 9 (shell): reset_world.sh guards empty GAME_NAME.
  - Round 10 (cross-cutting): scaling cmd rejects <3 distinct load levels; speed cap skipped at pace<=0 (would freeze bots).
- 10 ROUNDS COMPLETE: 4 parallel adversarial reviewers x2 batches + own bridge/shell/cross-cutting passes. ~20 findings verified (dropped false positives: NameError claim, _updateStart race, capture:411). Fixed 1 CRITICAL (Harmony hooks crash game tick), 1 security (codec OOB), and correctness/robustness issues across both repos. All green: 44 py tests, ruff+mypy+shellcheck, bridge + loadgen build, golden-wire + selftest PASS.

## Adversarial code review - second pass (10 rounds, fresh areas)
- 4 fresh parallel reviewers (foundation, flame+capture, join-path, re-review-my-changes) + own passes (JitMap, build scripts, cross-cutting). Verified findings, dropped false positives (runner timeout would break long captures; DoS-on-own-generated-files; benign atomic-primitive State races).
- HIGH/CRITICAL fixed:
  - GameJoinClient.cs: NetManager socket + memory LEAK - ActiveNets was a ConcurrentBag (never removed -> unbounded growth across rejoins) and the poll/action loop had no try/finally (exception -> leaked UDP socket). At 1000-bot scale = fd/memory exhaustion. Now a removable ConcurrentDictionary + StopNet() helper called on every exit path via try/finally.
  - JitMap.cs: assembly.GetTypes() threw ReflectionTypeLoadException (routine across mscorlib/Unity in full mode), aborting the whole jitmap. Now SafeGetTypes falls back to loadable types.
  - capture.py: umount failure was silently swallowed -> orphaned bind mount blocks the next run's mount; now warns loudly. Added timeouts to preprocess_bt (30s), make_flames (180s), tool_version (5s) subprocesses (hung child no longer blocks the pipeline).
  - report.py network_bound: pick the dominant disjoint subsystem by max(share), not list order (was assuming attribution is sorted).
  - stackcollapse_perf.py: frame-length cap (2048) stops O(N^2) regex backtracking; stack-depth cap (4096) bounds memory on corrupt/huge perf.data.
  - session.py audit: skip symlinks (no external file pulled into the integrity manifest; no dir-symlink-cycle hang).
  - budget.py: a section with no heat is omitted (UNKNOWN), never recorded as a healthy-zero that passes the gate.
- All green: 44 py tests + ruff + mypy + shellcheck; bridge + loadgen build; golden-wire + selftest PASS. Bridge reinstalled.

## Adversarial code review - third pass (fresh: web, preprocessors, HTML gens, deep concurrency)
- 4 fresh reviewers (web-endpoint security, preprocessors+scrapers, HTML/flame generators, deep concurrency+numerical) + own passes (WebApi/BridgeConfig, plans, Transfers-race). Deep concurrency audit came back CLEAN (Gate discipline, Metric ring, Transfers enum all correctly guarded; all analysis math verified correct).
- SECURITY fixed:
  - interactive_flame.py: STORED XSS - frame names (from JIT/perf) embedded in <script> via json.dumps; a name with "</script>" broke out and executed when the report is opened. Now \u-escaped; --title/--speedscope-name html.escape'd. Verified: 0 breakouts, escaped.
  - flamegraph.py: same XSS class is display-only (escaped), but added float()/empty-frame guards (malformed folded line no longer crashes).
  - preprocess_bt.py: bpftrace INJECTION - the output runs as ROOT via sudo; --comm with a quote or --mono-so with ':'/brace injected root code. Now validated (reject string-literal breakers in comm; mono-so must be a plain existing path). Verified: injection rejected, real space-free bind path accepted, 11 probes still compile.
- Robustness fixed:
  - proc_sample.py + threads.py: /proc reads race with process/thread exit (TOCTOU after the exists() check) -> IndexError/ValueError crashed the sampler mid-run; now guarded (skip/break, keep zeros).
  - BridgeConfig.cs: SpikeThresholdMs unclamped - a <=0 value marks EVERY frame a spike -> SampleWorld() + AddSpike every tick (heavy per-frame server cost). Clamped to [1, 60000] ms.
  - app_scrape.py: inter-chunk telnet silence window 0.15s -> 0.5s; a laggy server (scale scraping) stalled mid-reply and truncated large responses like `apm capabilities` (the truncated-JSON I hit earlier).
- Dropped as non-issues: Telemetry lastExportError leak (admin-only endpoint, hurts debug to hide), correlate.py "ReDoS" (single literal = linear, optional tool), report.py gm_avg=0 (only 0 when data missing, skip is correct), WebApi permission (0 = admin, enforced by framework).
- All green: 44 py tests + ruff + mypy + shellcheck + 11 bpftrace probes; bridge builds + installed.

## Adversarial code review - fourth pass (semantics, error/signal paths, frontend, regression)
- 4 fresh reviewers (frontend JS/speedscope, analysis SEMANTICS/math, error+signal paths, regression re-review) + own passes (interrupt cleanup, deep-scale, shell). Two agents came back "no issues" (frontend safe - React auto-escapes, _esc consistent, speedscope indices correct; math all correct - deep-scale, KB/tick units, headroom%, late_share, futex share, main_thread_share).
- REAL bugs fixed:
  - bridge.py attribute_subsystems: SEMANTIC - frame_core_exclusive was computed but never written back to totals, so the share DENOMINATOR double-counted the nested buckets (frame_core inclusive + its children) -> all subsystem shares deflated, didn't sum to 100%. Fixed: use exclusive frame_core in totals. Verified shares now sum to 1.0 (network 62.9% at 498 players). Test added.
  - capture.py _unmount_mono: REGRESSION from my own last-pass fix - added timeout=15 but did not catch TimeoutExpired; it would propagate out of run_capture's finally, crashing cleanup AND leaking the mount. Now wrapped (warn, never raise).
  - cli.py scenario_run: added except KeyboardInterrupt (Ctrl-C now sets rc=130 + still tears down loadgen); bounded the final post-kill load_process.wait() with timeout=5 (was unbounded - could hang on uninterruptible sleep).
- Verified sound (no fix needed): capture SIGINT path (SIGTERM handler + finally unmount + killpg with start_new_session process groups), Transfers dict race (both under Gate), all analysis math. Regression agent confirmed ALL prior-pass fixes correct (net-leak try/finally, XSS escaping, preprocess regex, network_bound max).
- Dropped: Popen/append interrupt race (negligible window + bpftrace `timeout` self-terminates), finalize atomicity (atomic_json + resilient _load already cover it).
- All green: 45 py tests + ruff + mypy + shellcheck + 11 probes; bridge + loadgen build; selftest PASS. Bridge reinstalled.

## Adversarial code review - fifth pass (boundary/fuzz, gate/prometheus, scale orchestration, state/time)
- 4 fresh reviewers + own passes (IP alloc, results race, gate release, ramp math, jitsym bisect - all verified correct). Boundary/fuzz agent: "no issues" (all parsers boundary-safe). Loadgen orchestration: confirmed ConcurrentBag/semaphore/IP-range safe.
- Fixed:
  - GameJoinClient.cs: bot HANG - a PlayerId packet with entityId<=0 fell through both branches, leaving the bot at PlayerIdReceived (never Joined) until the up-to-1h timeout; now Fails fast. At scale that wasted a thread+socket per stuck bot.
  - index.py: XSS - session dir name went into href+text unescaped (a maliciously-named session dir could inject script into index.html); now html.escape'd. + guard malformed summary (non-dict layers no longer crash the whole index).
  - compare.py: regression-gate correctness - reject captures < 5s (a failed 0s capture vs a real one gave meaningless deltas); a section measured in only one session is flagged "not_comparable" instead of showing a false improvement (0 heat).
  - cli.py prometheus: proper Prometheus label escaping (\ then " then newline) via _prom_label - the old code only stripped quotes (lost data, and a newline would break the line format). Labels are fixed enums today but a metrics exporter must emit valid text.
  - session.py _date: always return AWARE UTC - a naive result (ISO string without tz) would TypeError when compared to the aware bridge snapshot stamp (capture.py:856).
  - app_scrape.py: monotonic deadline (was wall-clock; a clock shift cut the window short/long); per-record timestamp stays wall-clock.
  - Program.cs rejoin loop: recompute `remaining` fresh (was stale -> overshoot timeout by one join); added deterministic clientId-based backoff+jitter so 1000 bots failing together don't retry in unison (thundering herd); clamp --ramp-ms.
- Dropped: prometheus NaN/Inf value guards (values provably bounded), TIME_WAIT accumulation (mitigated by the new backoff), session._date now()-fallback (informational field).
- All green: 45 py tests + ruff + mypy + shellcheck + 11 probes; bridge + loadgen build; selftest PASS; index XSS + prometheus smoke verified.

## Adversarial code review - sixth/seventh/eighth passes (10 fresh reviewers + own verification)
Fresh lenses per wave; every finding independently verified, false positives dropped with reasons. All three repos green throughout (46 py tests + ruff + mypy + shellcheck + 11 probes; bridge + loadgen build; selftest PASS; index XSS + prometheus smoke).

Wave 6 (bridge threading / analysis numeric / loadgen wire+net / capture process):
- Telemetry.cs: `_lastExportError` -> volatile. Written on main thread (SampleWorld, outside Gate) and export thread (Write/catch), read at Snapshot under Gate; locking only the read gives no ordering. Reference-atomic (no torn value/crash) but a real cross-thread visibility race; volatile is the acquire/release fix.
- PackageCodec.cs TryInflate: 64MB output cap on decompression. A malformed/hostile compressed frame could decompression-bomb the host and OOM all 1000 bots at once; overshoot now treated as a failed decode.
- capture.py (x2): wrap stream.close() in suppress(OSError). A flush failure (disk full) in the finally could skip _unmount_mono and leak the bind mount into the next capture; the Popen-error path could skip the error record. (Same cleanup-ordering class as pass 4.)
- Analysis numeric review: "no issues" (all divisions guarded, log-log least-squares correct at <3 pts / zero x-variance / y<=0).

Wave 7 (dashboard+WebApi security / bpftrace probes / pydantic+io / loadgen determinism):
- futex.bt: removed dead `@wait_addr[tid]` (set, never read, never deleted) - per-tid map entry allocated for nothing.
- session.py: audit must REPORT a malformed session, not crash on it. Added `_int()` (non-numeric meta.pid no longer raises before the manifest is written) and type-guarded `only` (a non-string like a JSON list no longer str()-mangles into corrupt layer tokens). +regression test.
- Dashboard/WebApi security review: "no issues" (level-0 admin gate, React auto-escape, Jinja2 autoescape, GET-only, index html.escape).
- Dropped: 11 bpftrace "unbounded map" reds (BPF maps are pre-sized/drop-on-full, `@[ustack]=count()` aggregation is intended and clear() would delete the profile, all `--dry-run` validated); loadgen "all bots same ActionSeed" red (false: clientId+life folded in at GameJoinClient:376); telnet injection (line-oriented console, capture excludes \s incl newline); reset_world pgrep -x truncated comm (correct); pydantic bound tightenings + io size/nan guards (self-generated data, risk of rejecting valid old sessions).

Wave 8 (bridge lifecycle+reflection / loadgen state-machine deep / report+HTML generation):
- index.py: escape EVERY dynamic row cell via a `_cell` helper (utc, pid, entities/players, health, grade, verdict, profile, grossAlloc/STW). Only dir/link/verdict were escaped before; the rest render raw from unvalidated health.json/summary.json/meta -> 8 stored-XSS sinks (same write-a-session-dir threat model as the pass-5 dir fix). Verified 0 raw injections on a crafted health.json.
- GameJoinClient.cs (3): (a) ignore a duplicate PlayerId once joined - it was overwriting the established EntityId and would break all entity-keyed packet routing for the session (restructured so entityId<=0 still fails fast); (b) stage-guard BOTH PackageIds recognition paths - a second PackageIds packet re-ran ApplyPackageMappings (which Clear()s the id table) mid-session; (c) reject non-finite server positions - Infinity passed the py>1f check (and px/pz were never range-checked) and poisoned PosX/Y/Z into every subsequent move.
- BridgeMod.cs: null-check the GameManager type before AccessTools.Method in PatchFrame, matching the other patch sites - removes any NRE-at-mod-load risk on a game build missing the type.
- BridgeConfig.cs: upper-clamp PeriodicExportSeconds to 3600s so a typo'd huge value can't silently disable periodic export.
- Dropped: ParsePosAndRotBody truncated-quaternion (documented + caught, BinaryReader bounds-checks, harmless discard); inbox drop-oldest (correct bounded queue, bursts are post-join); respawn "timeout" log-timing nit (bounded 15s, cosmetic, break either way).

## Adversarial code review - ninth pass (C# metric math / Python concurrency / loadgen long-uptime / lag-diagnosis logic)
- bridge.py stall_correlation: a zero-duration frame spike resolved `0 >= 0` at the final classifier and was falsely blamed on a GC pause. The pass-1 guard only covered the search loop; added `spike_ms > 0` to the classification too -> "unknown".
- cli.py loadgen shutdown: after the bounded SIGKILL wait, added a `poll()` to reap a child that exits right after the kill (was leaving a zombie until CLI exit; a true D-state child can't be reaped by anyone).
- Program.cs summary: cast per-bot int action counts to long before `.Sum()` across up to 1000 bots. Sum(int) is unchecked and wraps silently to a negative total on a multi-day run (1000 x ~1.7M walks/day > int.MaxValue).
- Clean bills: Telemetry metric math "no issues" (ring buffer wrap, p50/p95/p99 rounding, transfer accounting, deep-sample semantics all correct). Lag-diagnosis thresholds/health weights (sum to 1.0)/attribution argmax all consistent.
- Dropped: capture.py SIGKILL "zombie" (the very next line polls and reaps, and run_capture already warns on still-draining children); per-bot int counters (fine for ~1000 days at realistic pace; only the cross-bot aggregate needed long).

## Bridge webui (live metrics) - requested improvement
- WebMod/bundle.js: replaced the 4-cell panel with a live dashboard driven by the existing /api/apm snapshot (no new deps; still React.createElement so text stays auto-escaped). Adds a health pill (HEALTHY/DEGRADED/SATURATED from TPS + late-tick share), computed TPS, tick avg/max, gmUpdate avg/max, late ticks + stall ms, players/clients, entities + live AI, GC gen0/s and gen2/s, gross alloc MiB/s (amber over 200 MiB/s), heap, working set, thread count, dropped exports, and a window line. Sections table now shows p50/p95/p99 with rows shaded amber/red over the 5/16 ms budget; added a recent-spikes table (last 12 with world context). Refresh 5s -> 2s.
- WebMod/styling.css: pill + status colors, header flex layout, over-budget row markers. Verified: node --check passes, dist ships the updated WebMod, bridge build clean.

## Adversarial code review - tenth pass (perf/flamegraph post-processing / PackageCodec encode side / hook bodies)
- events.py build_timeline: a non-numeric "t" in any collector record (corrupt or format-changed JSONL) made `float(e["t"])` crash the ENTIRE timeline build. Added safe coercion `_t()`; a bad timestamp now falls to untimed instead of raising.
- stackcollapse_perf.py FRAME regex: accept an optional `0x` address prefix. Default `perf script` emits bare hex (current regex was correct), but some perf versions / -F formats prefix with 0x; the change is strictly more permissive, zero risk.
- ActionLoop.cs: guard Move/SendFlags/DoDrown/DoSuicide/DoKilled on a zero package id (unmapped type) so they skip instead of emitting id=0 (which the server misroutes to its PackageIds parser). Matches the existing chat-action guard. Unreachable on a conformant server (core packets are always mapped) but closes the inconsistency.
- Clean bills: hook bodies "no issues" (per-__state nesting for reentrancy, Harmony postfix-on-exception pairing, 1-in-N deep-sample counter via Interlocked, TryGetValue id lookup). Metric math "no issues" (wave 9).
- Dropped: 4 NaN/Inf-encode risks (position sources guarded finite in wave 8, movement deltas Math.Clamp'd, angles PackAngle256-wrapped -> non-finite unreachable); hardcoded damage/spawn field layout (the reverse-engineered protocol's design, covered by golden-wire tests).

## Bridge webui - live-metrics features (all requested items implemented)
- Sparklines: rolling 60-sample client-side history (deduped by snapshot utc, no server change) drawn as inline SVG for TPS, gross alloc MiB/s, and gmUpdate avg ms.
- Section attribution: added a "% of 50ms" column with an inline bar per managed section (avgMs/50ms), shaded amber >10% / red >32%.
- Tick-budget bar: current tick avg vs the 50ms (20 TPS) budget as a colored bar.
- Freeze + Copy JSON: pause the live refresh to inspect a frozen snapshot; copy the raw snapshot to clipboard for bug reports.
- Sections table: substring filter box + clickable sortable column headers.
- Leak-signal shading: GC gen2/s and heap cells go amber when the window tail averages >1.2x the head (climbing), using the same client-side history.
- Panel stays dependency-free (React.createElement, runtime-provided React hooks) and read-only (WebApi is GET-only admin; no mutating controls added). node --check passes; dist ships the updated WebMod; bridge build clean.
- R26 (2026-07-18): ran both scale ladders, closing the pending ladder/attribution items above. PLAYER-scale fit (15->498 players, no zombies, `apm scaling --by players`): network layer is super-linear - `ConnectionManager.Update` O(N^2.27) and `NetEntityDistribution.OnUpdateEntities` O(N^2.26) per call; entity AI sub-linear (confounded - no zombies spawned). ENTITY/zombie-scale fit (players held at 16, zombies 114->452 via `plans/scale_ladder.py`, `apm scaling --by entities`): entity AI is LINEAR - `World.TickEntities` O(N^1.13) per call; NO super-linear section by entities; `ConnectionManager.Update` sub-linear (0.09) in entities (player-driven). So two distinct walls: player=network super-linear, zombie=AI linear-volume. Also confirmed entity sim is observer-gated (0 players -> entityAlives=0, TickEntities ~0.005 ms/call, ~415 zombies persist dormant). Zombie population plateaus ~450-500 (scout despawn). Bridge fix this session: dropped `NetConnectionSimple.taskSerialize` from instrumentation (long-lived writer-thread task reported 600s+ lifetime, swamped attribution) + added a 30s per-sample drop guard. Full writeup: 7dtd-optimizer/docs/measured-scaling.md; optimizer OPTIMIZATION_CANDIDATES.md 4b/4d updated with exponents. Still deferred: A2 path-admission + TickEntity-stride before/after prototypes; reaching a true 1000-player capture (connect-pacing + host limits).

## Residual (V3.1.0, 2026-08-03)

| Residual | Status | Notes |
|---|---|---|
| Bridge on V3.1.0 dedi | **works** | Used for moderate/heavy/canon ES A/B |
| Forensic absolute budgets under spawn load | **expected FAIL** | Relative A/B is the product claim, not absolute forensic pass |
| Disk headroom for large forensic sessions | **ops** | Home often ~99% full; prune old sessions before 64p |
| TickEntities heat missing some arms | intermittent | Section skip when no heat samples; not a product bug |
| Compare stock vs EfficientServer one-click report | optional | Manual session pair + V310_APM_BASELINE today |
