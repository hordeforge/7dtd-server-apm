# Optional in-game instrumentation bridge

`7dtd-server-apm-bridge.dll` is an instrumentation-only 7DTD server mod. Host-only
capture remains supported without it. The bridge adds managed subsystem timings,
world/runtime gauges, spike records, capability reporting, periodic atomic JSON,
and the `apm` console/telnet command.

It is also a native V3 WebDashboard plugin. `WebMod/` adds a direct **7DTD APM**
sidebar entry (a module route, not a Settings tab) and authenticated
`GET /api/apm` exposes the same bounded
snapshot used by console capture. The endpoint defaults to administrator
permission level 0. There is no config switch for it: browse to the port the
server's own WebDashboard is configured on (8080 in the loadgen profile) and
sign in normally; the mod opens no separate web listener. The menu entries are
registered unconditionally (the session
cookie is HttpOnly, so client-side JS cannot see it to gate registration); a
logged-out or non-admin visitor sees the entry and the panel's
"Authentication required" state after its first poll answers 403, since the
endpoints stay at permission 0. The bridge exposes measurement only: there is
no endpoint that writes config or restarts the server (a former
`GET/POST /api/perf` ops switch for the sibling EfficientServer mod was
removed in 2.5.0; APM measures and never edits optimizer config).

### Mod config

`Config/apmbridge.json` beside the DLL, seeded on first install from
`apmbridge.json.example` (`//` and `/* */` comments are accepted). `apm reload`
re-reads it; `DeepMode` is the one key that needs a server restart to take
effect, and the reload says so when it changes. A value outside the accepted
range is clamped, and a file that cannot be parsed, or that carries an unknown
key, is rejected: the mod logs the reason and runs built-in defaults rather
than silently profiling under settings nobody asked for. The startup line
(`[7dtd-server-apm] config: ...`) names the file it read and the values in
force, so the active config is readable from the server log.

| Key | Type | Default | Accepted range |
|---|---|---|---|
| `Enabled` | bool | `true` | master switch; `false` loads no hooks |
| `DeepMode` | bool | `false` | adds the per-entity AI/path sections; needs a restart |
| `SpikeThresholdMs` | number | `50` | `1` to `60000`; a lower value records more spikes |
| `PeriodicExportSeconds` | number | `30` | `0` to `3600`; `0` disables periodic export |
| `LogPeriodicSummary` | bool | `true` | log line per periodic export |
| `LogSpikes` | bool | `true` | log line per spike |
| `MaxSpikeRecords` | int | `128` | `1` to `1024`; ring size kept in the snapshot |
| `DeepSampleRate` | int | `16` | `1` to `10000`; every Nth call in a deep section is recorded |

### Console command

`apm` (also spelled `apmbridge`) is the bridge's console verb set, available
wherever the game accepts console commands: the server console, a telnet
session, or a script. The verb list, the argument each verb takes, and the
help line are one table in `ConsoleCmdApm`, so the usage a caller reads and the
arguments the dispatcher accepts cannot disagree.

| Verb | Argument | Reply |
|---|---|---|
| `status` (default) | none | one-line window summary: `updates`, `gmUpdateAvg`, `tickAvg`, `spikes`, `sections`, `api`, `apiErrors` |
| `dump` | none | the path of a freshly written timestamped snapshot |
| `reset` | none | `APM reset` |
| `reload` | none | `APM config reloaded`, or the parse error when the config file was rejected |
| `capabilities` | none | indented JSON: per-hook status and the game assembly identity |
| `jitmap` | `full` (optional, case-insensitive) | the jitmap path with `symbols=` and `skipped=` counts |
| `benchmark` | iterations, `1000` to `1000000`, default `100000` | `iterations=`, `nsPerRecord=`, `budgetNs=`, `pass=` |

A call the table does not accept is refused, never quietly run as a different
one: an unknown verb, a stray argument to a verb that takes none, an option
outside `jitmap`'s single `full`, and a non-integer or out-of-range benchmark
count each answer with the offending value and the usage line. `apm jitmap
FULLL` used to produce the short map, and `apm benchmark 10` used to be clamped
up to the 1000-iteration floor, so a mistyped call answered like a successful
one that measured something else. A failure inside a verb answers `APM error:
<message>`. The console has no exit status, so a scraper reads the reply text
and treats those lines as the failure signal.

`dump`, `reset`, and `reload` change server state, and the bridge authorizes no
console verb itself: any account with console access reaches them
(`docs/THREAT_MODEL.md` R7). All three are safe to repeat. `dump` never
overwrites an existing dump (it suffixes on collision), and `reset` and
`reload` re-apply the same state. The other verbs only read.

### Web authorization matrix

| Endpoint | Verbs | Required level | Notes |
|---|---|---|---|
| `/api/apm` | GET | 0 (admin) | read-only telemetry snapshot; newest 12 spike records, the panel's row count |
| Any verb other than GET | 0 + not implemented | the base handler answers 405 |

Enforcement is not per-handler code: every `AbsRestApi` subclass registers its
per-method required levels in `AdminWebModules` at construction, and the
dashboard's API host checks them centrally before any handler runs (403
otherwise). The endpoint declares `{0,0,0,0,0}`: every verb requires level 0,
and HEAD/OPTIONS are denied outright by the framework's array padding. It
accepts no object identifiers, so there is no object-level access surface, and
it performs no writes of any kind.
Every bridge REST class must keep an explicit all-zero
`DefaultMethodPermissionLevels` override; `test_bridge_build_surface.py` fails
otherwise so widening access cannot happen by silently dropping a default.
The panel JS is TypeScript (`WebMod/bundle.ts`), compiled to `bundle.js` by
the version-pinned `bunx` TypeScript path inside `make bridge-build`; do not
hand-edit the generated bundle. Bun is required, but no global `tsc`
installation is needed. The stock dashboard loads `bundle.js` and
`styling.css` on every page, so they are the panel's entire download: the
emit strips comments (the source of record is `bundle.ts`), and
`test_bridge_build_surface.py` budgets both files. A change that needs more
raises the budget in that test with the measurement behind it.

### Response contract

`GET /api/apm` answers one self-describing document: the same snapshot the
console and the on-disk `apm_app_*.json` carry, schema `7dtd.apm.app.v3`. It
takes no parameters, so a client never has to build a query string, and it
accepts no request body.

| Status | Body | Cause |
|---|---|---|
| 200 | snapshot (`schema: "7dtd.apm.app.v3"`) | normal poll |
| 403 | framework auth error | session missing, expired, or not permission level 0 |
| 500 | error envelope with `"code": "SNAPSHOT_FAILED"` | snapshot could not be built; nothing partial is returned |

The 500 path never answers with a half-built snapshot: a client can treat any
document carrying `schema` as usable evidence and any error envelope as no
evidence at all. Every `utc` field (top level, `world`, `spikes[]`) is an
ISO-8601 UTC instant or `null`; no field ever carries a placeholder string in a
date slot. `world.utc` is `null` until the first world sample is taken (first
spike or first periodic export), so a `0` in `world.entities` before that point
is an unmeasured world, not an empty one.

A 200 carries `Cache-Control: no-store`. The document is a live sample taken
when the request arrives, not a resource with a stable URL, so two polls a
second apart are different measurements; without the header a caching proxy in
front of the dashboard port may store a response that carries no freshness of
its own and replay a stale snapshot as a current one. There is no `ETag`: a
client that sent one would be owed a `304` path this endpoint does not
implement, and a conditional poll would only ever return stale telemetry.

| Key | Contents |
|---|---|
| `capabilities` | hook status per patched method, game assembly identity |
| `measurement` | measured method name, duration unit, deep sample rate |
| `update` | `gmUpdate` and server-tick durations, late ticks, spike count |
| `health` | export queue state, dropped exports, per-source last error, `/api/apm` request and failure counts |
| `host` | `/proc` load, memory, uptime, RSS; `null` on a non-Linux or unreadable host |
| `gc` | window-scoped collection counts, heap, gross allocation rate |
| `world` | last sampled clients, entities, alive entities, players, working-set bytes, thread count, frame delta |
| `mapTransfers` | per-package `packages`, `bytes`, `mebibytes` (the same `bytes` as a MiB float, so a client need not divide), `lastBytes`, `maxBytes` |
| `sections` | per-hook calls, avg/last/max/p50/p95/p99/total ms, `deep` flag; the `apm.api.snapshot` section times this endpoint |
| `spikes` | newest `DashboardSpikeRecords` (12) spikes, newest last, each with its own `world` sample |

A field that failed to read is never faked: each source names its own failure
in `health` (`lastExportError`, `lastSampleError` for the world sample,
`hostError` for the `/proc` reads behind a `null` `host`, `lastApiError` for
this endpoint), and each is cleared only by the next success of that same read,
so a working export cannot clear a failed sample. `gc.grossAllocBytesPerSecond`
is `-1` when no gross allocation counter exists on the runtime. A client thus
sees which numbers are unmeasured instead of a plausible zero. Add fields inside
the existing objects; the schema version changes only for a removed or retyped
field, since consumers parse the document with a strict section model
(`ManagedSectionV3`).

The endpoint instruments itself: `sections["apm.api.snapshot"]` carries the
request count and p50/p95/p99, `health.apiRequests` and `health.apiErrors` the
window totals, and a failure logs its exception type, message, and stack trace
(the coded `SNAPSHOT_FAILED` envelope carries no detail). It is the dashboard
panel's only data source, so a panel that stopped updating is otherwise
indistinguishable from a server that stopped working. `apm status` prints the
same two counts, and `apm reset` clears them with the rest of the window.

`spikes` is a tail, not the whole ring: the API serves the newest 12 records
(`Telemetry.DashboardSpikeRecords`) so a long spike streak cannot push 128
world samples down the wire on every 2 s poll. A client must not read a
12-element array as "that was every spike"; the periodic JSON export file
carries the full ring and is the surface to read for audit and compare.
Trimming the tail is not a removed or retyped field, so it does not move
`schema`.

Map delivery telemetry separates `ChunkManager.SendChunksToClients`, chunk and
map serialization, initial world-folder transfer, connection serialization, and
send-queue flushing. The snapshot and dashboard also expose per-package counts,
total bytes, and last/maximum package size under `mapTransfers`.

The `gc` block reports window-scoped Boehm collections, heap delta, and a gross
allocation counter (`grossAllocBytesPerSecond`). Gross comes from a native
P/Invoke of Boehm's `GC_get_total_bytes`, so it works on Unity 2022 Mono (which
lacks managed `GC.GetTotalAllocatedBytes`; that API is only the fallback, and
`-1` means neither was available). Without the bridge installed at all, the host
`mono_alloc` probe supplies gross allocation. Deep hooks
include tile-entity chunk load (`TileEntity.InstantiateFromRead`,
`TileEntityFeatureData.InstantiateModule`) so serialization cost is measurable
alongside the allocation churn it drives. Current schema `7dtd.apm.app.v3`,
mod version 3.0.0.

```bash
make bridge-build
make bridge-install
```

`make bridge-install` upgrades in place: shipped files that a later release
dropped are removed from the mod folder, and `Config/apmbridge.json` is never
overwritten. Every file the build staged is installed, not a fixed list, so a
new WebMod asset reaches the server without a second edit. If any file fails to
install, the previous release is restored from a pre-install backup (taken
before the prune, so a dropped file is put back) and the command exits nonzero;
operator settings under `Config/` are never touched by a rollback. A build that
staged nothing is refused before the mod folder is modified at all.

`make bridge-uninstall` removes the mod folder but moves that
config to `Mods/7dtd-server-apm-bridge-config.json` first, since the release
zip ships only the `.example` and the tuned settings cannot be regenerated.
A reinstall seeds a fresh factory config, so a second uninstall writes to
`7dtd-server-apm-bridge-config.json.1` (and `.2`, and so on) rather than
overwriting the config the first uninstall saved.

Restart the dedicated server, then run `apm capabilities`, `apm status`, or
`apm dump`. JSON is written under
`Mods/7dtd-server-apm-bridge/telemetry/` using `7dtd.apm.app.v3`.

Hooks are resolved by type and method name at startup. Missing hooks are marked
`unavailable` and do not prevent other instrumentation from loading. Deep AI and
path hooks are disabled by default because high-frequency timing has measurable
overhead. This mod requires EAC to be disabled and must be rebuilt/revalidated
after game updates.
