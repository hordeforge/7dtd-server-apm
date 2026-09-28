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
session after deliberately attaching any additional artifact. `audit` also
verifies every already-recorded artifact against its recorded hash: edited or
deleted evidence fails the audit (the recorded manifest is preserved so the
drift stays provable), while newly attached files are folded into a refreshed
manifest.

## Application evidence

Install the bridge documented in `bridge/README.md`. The telnet scrape issues
only `apm status`, `apm capabilities`, and `apm dump`, then copies a fresh
structured snapshot; a windowed capture additionally sends `apm reset`
(`--reset-bridge`) and `apm jitmap full` (automatic for `scenario run`,
opt-in via `capture --symbolize`), whose replies are not persisted.
EfficientServer is not an instrumentation dependency.

The telnet scrape persists only bridge command replies: the server's streamed
console-log lines (which name players, IPs, and Steam IDs) are filtered out in
the collector before anything reaches the session store, the raw scrape file is
excluded from export bundles, and session directories are owner-only.

The scrape log records every attempt, so a window whose telnet endpoint was
unreachable or whose password was rejected holds a full `app/bridge.jsonl` of
failures. Such a window is `app_sim unavailable`, not collected: the collector
result records the failure and the summary states why, so an empty app layer
never reads as a healthy zero.

## Controlled scenarios

```bash
uv run 7dtd-server-apm scenario run --seconds 60 --clients 6 --actions 500 --preset standard
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
  game code is actually hot). **Coverage:** unlike the bridge section
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
Exclusion is by file name, not by extension: `app/bridge.jsonl`, `perf.data`,
any `*.err`, the source `manifest.json`, and the
`runtime/libmonobdwgc-2.0.so` bind-mount placeholder the GC uprobes use all stay
out of bundles, as does any file whose name contains `efficientserver` or
`output_log`, so an operator-attached slice of the server log
(`app/efficientserver_log_excerpt.txt` or `OutputLog_2026-09-28.txt`) is dropped
whichever name it carries. Symlinks are skipped rather than followed. The
excerpt's section timings survive in `csharp_bridge.json`. Name exclusion is
the first layer, not the only one: every text member is also scrubbed by
content, and a line the game stamped with its console timestamp
(`2026-08-23T10:00:00 4020.512 INF ...`) is dropped whatever the file is
called, so an operator-attached console capture or chat log reaches a bundle
with its player lines removed. JSONL members are not line-filtered: a record
there is the tool's own structured telemetry, scrubbed field by field. Any
other text file an operator drops into a session is bundled, so check the
archive before sharing.
Inspect a bundle before sharing because game-derived artifacts may still
contain player or world data.

Raw sessions keep the full telnet drain in `app/bridge.jsonl` as owner-only
evidence (captured, scenario-run, and `import`-restored sessions are 0700 from
the creating syscall, store root included); raw evidence never enters export
bundles. The scrape discards the
telnet banner and post-logon reply, and persists only the requested `apm`
command responses. A streamed console-log line is cut at its timestamp
wherever that timestamp falls in the received line, not only when the line
begins with one, so a log line the server writes without a leading newline is
dropped along with its player names, IPs, and Steam IDs.

```bash
uv run 7dtd-server-apm export SESSION -o support.zip
uv run 7dtd-server-apm verify-store            # read-only restore check
uv run 7dtd-server-apm prune --keep 20 --dry-run
```

## Durability and recovery

The only durable state this tool owns is the session store
(`~/.local/share/7dtd-server-apm`, override `SEVENDTD_APM_DIR`): evidence directories
holding collector output, manifests, and reports. Everything else (repo,
bridge DLL) is reproducible from source. The store lives on one host disk; the
tool makes writes crash-safe (temp file + fsync + rename + directory fsync),
but it does not replicate or back up the store by itself.

| Disaster | What is lost | Recovery |
|---|---|---|
| Bad `prune --keep` / runaway auto-prune | Nothing within the grace window | `mv ~/.local/share/7dtd-server-apm/.trash/session_X ~/.local/share/7dtd-server-apm/` |
| Accidental file deletion inside a session | Files not yet trashed | Re-export/import from a bundle copy, or restore from the backup copy |
| Host disk loss | Whole store unless copied out | Copy the backup directory back over `SEVENDTD_APM_DIR`, then `verify-store` |

- **RPO:** the interval between two `backup` runs. Nothing copies the store by
  itself, so an unscheduled store has no bound at all: schedule the command.
  Each session lands in the destination through a staging rename, a rerun
  skips sessions whose recorded manifest hash is unchanged, and sessions
  pruned from the live store are not removed from the destination, so
  `prune --keep` cannot quietly shrink the archive.
  ```bash
  export SEVENDTD_APM_BACKUP_DIR=/mnt/backup/apm-store
  uv run 7dtd-server-apm backup                          # every night, from cron
  ```
  `SEVENDTD_APM_BACKUP_DIR` is the destination the scheduled run and the
  `doctor` check both read, so a backup that is verified is the backup that
  runs; the argument overrides it for a one-off.
  The destination should be another host or another filesystem; a directory
  on the same device is reported as `warning: ... same filesystem`, because
  disk loss then takes the copy with it. A destination the run creates is
  0700, like the store, because a session carries the raw telnet drain; a
  destination that already exists keeps the mode its owner gave it, so point
  the command at a directory of its own. `.scenario` manifests and the index
  are copied with the sessions; `.trash` is not (its contents are already
  retired evidence). Sessions still capturing (no `manifest.json` yet) are
  named as `skipped` and picked up by the next run, and a run that backs up
  nothing exits 1. Bundles made with `export` remain a second, sanitized
  copy; treat any bundle you keep as the recovery artifact for that session.
- **RTO:** minutes: sessions are self-contained directories; no service
  restart, migration, or schema step is involved in recovery.
- **Soft-delete window:** prune and post-capture auto-prune move removed
  sessions into `<store>/.trash/` before unlinking them
  (`APM_PRUNE_GRACE_HOURS`, default 24, `0` disables). Auto-prune keeps the
  newest `APM_KEEP_SESSIONS` (default 40, `0` disables). Expired trash is purged
  on later prune runs; trash never appears in listings or indexes.
- **Proven restore path:** `7dtd-server-apm import BUNDLE.zip` unpacks a sanitized
  export into the store, refuses unsafe archive members, and audits the result
  against the manifest the bundle carries before writing a fresh one: a member
  that drifted since the export is reported by name and the recorded manifest
  is kept, so a tampered bundle cannot be absorbed by the restore. Findings
  are printed one per line. Exported bundles are
  lossy by design (no raw telnet drain, perf data, or stderr), so prefer
  whole-directory copies for archival fidelity and bundles for sharing.
- **Restore drill (whole-store copies):** the `backup` copy is the archival
  path, and its integrity claim is only as good as the last drill. `backup`
  runs the drill itself: after copying it audits every session in the
  destination, including ones an earlier run copied, and exits 1 when any of
  them fails or when nothing was backed up at all.
  `7dtd-server-apm verify-store [STORE]` is the standalone form for a copy
  that arrived some other way (an rsync, a copy-back after disk loss). It
  audits every session in a store against its recorded `manifest.json` and the
  versioned schemas, then exits non-zero when any session is invalid. It
  writes nothing: `audit` re-stamps `manifest.json` on a clean session, which
  would absorb the very drift a restore check looks for, so it cannot be used
  on a restored copy. Run it on the copy after every restore, and periodically
  against the live store.
  - `ok`: every recorded artifact matches its hash, required documents present,
    schemas valid.
  - `INVALID`: hash drift, a schema failure, or a recorded path that escapes the
    session. Restore is incomplete; re-copy the affected session.
  - `incomplete`: a session still capturing, or one copied before `finalize`
    and with no `manifest.json` recorded, so its hashes were never baselined.
    Not a failure, and `--strict` fails the drill on it.
- **Silent backup failure:** alert on the `backup` exit code (a copy that
  cannot be made, a destination that does not verify, or a run that backed up
  nothing all exit 1). `doctor` carries the same signal as the
  `checks.store_backup` block: `ok: false` when no destination is configured,
  when nothing has been copied, or when the last run recorded no session, and
  `age_seconds` for the threshold your schedule implies. A store that stops
  growing is indistinguishable from a quiet server, and a destination that
  quietly stopped syncing looks exactly like a healthy one, so age the store
  as well (for example
  `ls -lt --time=ctime ~/.local/share/7dtd-server-apm | head`).

## Related docs

| Doc | Role |
|---|---|
| [FEATURES](FEATURES.md) | Capability surface |
| [APM_CS_BRIDGE](APM_CS_BRIDGE.md) | Managed correlation |
| [COMPATIBILITY](COMPATIBILITY.md) | Supported matrix |
| [THREAT_MODEL](THREAT_MODEL.md) | Attack surface, trust boundaries, controls |
| [LOAD_PROFILE](LOAD_PROFILE.md) | Canonical compare workload |
| [ROADMAP](ROADMAP.md) | Backlog |
| Loadgen | [`../../7dtd-loadgen/docs/README.md`](../../7dtd-loadgen/docs/README.md) |
| Host topology | [`../../7dtd-server-optimizer/docs/HOST_TUNING.md`](../../7dtd-server-optimizer/docs/HOST_TUNING.md) |
| Measured scale laws | [`../../7dtd-server-optimizer/docs/measured-scaling.md`](../../7dtd-server-optimizer/docs/measured-scaling.md) |

## Changelog

- **2026-08-23:** Durability pass: prune trash window, `import` restore command, recovery runbook.
- **2026-07-19:** Ownership header; related docs.
