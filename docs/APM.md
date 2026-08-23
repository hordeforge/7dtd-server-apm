# Capture and evidence model

**Owns:** what a capture is, validity rules, application evidence, lag diagnosis.  
**Not:** CLI feature list ([FEATURES](FEATURES.md)), bridge install detail ([APM_CS_BRIDGE](APM_CS_BRIDGE.md)), workload pins ([LOAD_PROFILE](LOAD_PROFILE.md)).

A capture is a bounded observation of one server PID. Host collectors write raw
artifacts while the optional bridge writes managed method, tick, world, GC, and
spike snapshots. Finalization derives evidence only after collectors stop.

## Validity

Every requested layer is `collected`, `failed`, `unavailable`, or `skipped`.
Only collected evidence participates in scoring. Health grades require at least
80% weighted coverage. Budgets do not pass unknown layers, and comparisons
reject different layer sets, collector selections, or durations differing by
more than 10%.

The integrity manifest is written after finalization output closes. Re-audit a
session after deliberately attaching any additional artifact.

## Application evidence

Install the bridge documented in `bridge/README.md`. Host capture issues only
`apm status`, `apm capabilities`, and `apm dump`; it then copies a fresh
structured snapshot. EfficientServer is not an instrumentation dependency.

## Controlled scenarios

```bash
uv run 7dtd-apm scenario run --seconds 60 --clients 6 --actions 500 --preset standard
```

The scenario command calls `../7dtd-loadgen/scripts/run_loadgen.sh`; it
contains no client/protocol implementation. The loadgen run manifest is attached
as `workload.json` and included in the final audit.

## Lag diagnosis ("laggy without CPU")

Finalization synthesizes a `lag_diagnosis` (verdict + ranked causes with fixes)
in `summary.json` metadata, surfaced on the dashboard. Causes include
`gc_pauses`, `main_thread_bound`, `lock_contention`, `chunk_bandwidth`,
`memory_growth`, and disk/scheduler stalls. Key evidence sources:

- **Gross allocation churn.** `gc.grossAllocMBPerSecond` is the true GC-pause
  driver. Net heap growth (`allocMBPerSecond`) reads ~0 at steady state because
  allocation and collection cancel, so it masks the problem. The bridge reports
  gross in every capture by P/Invoking Boehm's native `GC_get_total_bytes`
  (cheap, no probe). Unity 2022 Mono lacks `GC.GetTotalAllocatedBytes`; when the
  bridge is absent, the opt-in `mono_alloc` probe (`--only all,alloc`) supplies
  it from `GC_malloc`. Left UNKNOWN (omitted) when unmeasured; the budget never
  treats absence as a healthy zero.
- **Stop-the-world pause timing.** The `mono_gc` probe times
  `GC_stop_world`→`GC_start_world` (the exact main-thread freeze):
  `stw_pause_worst_ms` / `_total_ms`. The diagnosis distinguishes rare big STW
  freezes (high load) from constant incremental `collect_a_little` drain
  (moderate load) driven by the same churn.
- **Allocation attribution.** The forensic capture (`--only all,alloc`) names the
  sites behind the churn: large-alloc spikes (`top_alloc_sites`, e.g.
  `AstarVoxelGrid.InitScan`) and the steady small-object churn floor
  (`top_churn_sites`, sampled 1/4096), resolved to method names via the bridge
  jitmap. The `alloc` probe is opt-in and needed for the site names, not the gross
  rate (which the bridge provides by default); name it alongside `all` so the
  managed section table is still captured (`--only alloc` on its own drops it).
  Attribution ranks each `ustack` record by **total bytes** and attributes it to
  the first **game** frame under the `GC_malloc` leaf, skipping BCL/runtime/
  profiler noise (`System.*`, `Unity.Profiling`, unresolved hex). This matters:
  bpftrace prints maps *ascending*, so a naive top-down read of the block returns
  the smallest stacks' BCL leaves (`String.Split`, `GameTimer.Reset`) instead of
  the real owners. That was the bug that briefly hid the true heaviest sites
  (`AstarVoxelGrid.InitScan`, `PooledBinaryWriter.Write`); fixed 2026-07-18 in
  `report._alloc_block_sites`, regression-tested.
- **CPU hot-path ranking (auto-discovery).** `cpu_hot_paths` in `summary.json`
  metadata ranks the **symbolized perf folded stacks** (`cpu/perf/stacks.folded`,
  managed frames resolved via the jitmap) into two views: `inclusive` (functions by
  total samples anywhere in the stack, native kept) and `self_game` (the leaf sample
  attributed to the first **game** frame, skipping native/GC/BCL noise - i.e. which
  game code is actually hot). This is **comprehensive**: unlike the bridge section
  timings (a *curated* set of Harmony-hooked methods), it surfaces every hot method
  perf sampled - e.g. `StreamUtils.StreamCopy`, `ChunkBlockLayer.GetAt`,
  `Lighting3DArray.GetLight`, the writer-thread serialization cluster. **Caveat:**
  perf samples **all threads across all cores**, so `inclusive`/`self_game` are
  aggregate CPU (the single 20 TPS sim thread is a small fraction - `GameManager.Update`
  reads ~0.6%). A third view, **`main_thread`**, ranks only the sim thread's samples
  (`stacks.main.folded` = `perf script --tid=<pid>`, tid==pid is the Unity main/sim
  thread) - the hot game code **for the tick itself** (what gates `ms_per_tick`):
  `GameManager.Update`, `SendToPlayers`, per-entity `OnUpdateLive` / `EntitySeeCache.CanSee`
  (AI vision), `KinematicCharacterMotor` (physics), `AstarVoxelGrid.CalcBlockingFlags`
  (nav scan), `GetClosestPlayer`. Use `main_thread` for the tick bottleneck, `self_game`/
  `inclusive` for total-CPU hot spots (serialization, GC, array-init), and the bridge
  sections for precise per-method tick timing. Complementary; none alone is the whole picture.
- **Chunk bandwidth.** Reported from the kernel `udp_sendmsg` byte sum (always
  capture-windowed, the honest current rate). The bridge `mapTransfers` MB/s is
  a since-reset lifetime average inflated by the join burst and is shown for
  context only, not gated.

## Security and retention

Use `SEVENDTD_TELNET_PASSWORD`. Sanitized exports omit raw telnet responses,
perf data, stderr, command lines, and executable paths, and scrub the host home
prefix from JSON, JSONL, bpftrace output, flamegraph SVG, and other text
artifacts. Event timelines carry only extracted bridge metrics, never raw
console text (the telnet stream can contain player names, IPs, and Steam IDs).
Inspect a bundle before sharing because game-derived artifacts may still
contain player or world data.

Raw sessions keep the full telnet drain in `app/bridge.jsonl` as owner-only
evidence (sessions are chmod 0700); it never enters export bundles. The scrape
itself discards the telnet banner and post-logon reply and persists only the
requested `apm` command responses.

```bash
uv run 7dtd-apm export SESSION -o support.zip
uv run 7dtd-apm prune --keep 20 --dry-run
```

## Durability and recovery

The only durable state this tool owns is the session store
(`~/.local/share/7dtd-apm`, override `SEVENDTD_APM_DIR`): evidence directories
holding collector output, manifests, and reports. Everything else (repo,
bridge DLL) is reproducible from source. The store lives on one host disk; the
tool makes writes crash-safe (temp file + fsync + rename + directory fsync),
but it does not replicate or back up the store by itself.

| Disaster | What is lost | Recovery |
|---|---|---|
| Bad `prune --keep` / runaway auto-prune | Nothing within the grace window | `mv ~/.local/share/7dtd-apm/.trash/session_X ~/.local/share/7dtd-apm/` |
| Accidental file deletion inside a session | Files not yet trashed | Re-export/import from a bundle copy, or restore from your host backup |
| Host disk loss | Whole store unless copied out | Copy-back from off-host backups or shared support bundles |

- **RPO:** unbounded for the local store unless an external copy exists.
  Schedule one (`rsync -a ~/.local/share/7dtd-apm/ backup-host:/apm-store/`
  from cron/systemd timer); sessions are immutable after finalize, so rsync is
  incremental and safe to re-run. Bundles made with `export` are a second,
  sanitized copy; treat any bundle you keep as the recovery artifact for that
  session.
- **RTO:** minutes: sessions are self-contained directories; no service
  restart, migration, or schema step is involved in recovery.
- **Soft-delete window:** prune and post-capture auto-prune move removed
  sessions into `<store>/.trash/` before unlinking them
  (`APM_PRUNE_GRACE_HOURS`, default 24, `0` disables). Expired trash is purged
  on later prune runs; trash never appears in listings or indexes.
- **Proven restore path:** `7dtd-apm import BUNDLE.zip` unpacks a sanitized
  export into the store, refuses unsafe archive members, runs the same audit
  as `finalize`, and writes a fresh integrity manifest. Exported bundles are
  lossy by design (no raw telnet drain, perf data, or stderr), so prefer
  whole-directory copies for archival fidelity and bundles for sharing.

## Related docs

| Doc | Role |
|---|---|
| [FEATURES](FEATURES.md) | Capability surface |
| [APM_CS_BRIDGE](APM_CS_BRIDGE.md) | Managed correlation |
| [COMPATIBILITY](COMPATIBILITY.md) | Supported matrix |
| [LOAD_PROFILE](LOAD_PROFILE.md) | Canonical compare workload |
| [ROADMAP](ROADMAP.md) | Backlog |
| Loadgen | [`../../7dtd-loadgen/docs/README.md`](../../7dtd-loadgen/docs/README.md) |
| Host topology | [`../../7dtd-optimizer/docs/HOST_TUNING.md`](../../7dtd-optimizer/docs/HOST_TUNING.md) |
| Measured scale laws | [`../../7dtd-optimizer/docs/measured-scaling.md`](../../7dtd-optimizer/docs/measured-scaling.md) |

## Changelog

- **2026-08-23:** Durability pass: prune trash window, `import` restore command, recovery runbook.
- **2026-07-19:** Ownership header; related docs.
