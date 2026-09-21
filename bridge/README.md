# Optional in-game instrumentation bridge

`7dtd-server-apm-bridge.dll` is an instrumentation-only 7DTD server mod. Host-only
capture remains supported without it. The bridge adds managed subsystem timings,
world/runtime gauges, spike records, capability reporting, periodic atomic JSON,
and the `apm` console/telnet command.

It is also a native V3 WebDashboard plugin. `WebMod/` adds a direct **7DTD APM**
sidebar entry (a module route, not a Settings tab) and authenticated
`GET /api/apm` exposes the same bounded
snapshot used by console capture. The endpoint defaults to administrator
permission level 0. Enable `WebDashboardEnabled`, browse to its configured port
(8080 in the loadgen profile), and sign in normally; the mod opens no separate
web listener. The menu entries are registered unconditionally (the session
cookie is HttpOnly, so client-side JS cannot see it to gate registration); a
logged-out or non-admin visitor sees the entry and the panel's
"Authentication required" state after its first poll answers 403, since the
endpoints stay at permission 0. The bridge exposes measurement only: there is
no endpoint that writes config or restarts the server (a former
`GET/POST /api/perf` ops switch for the sibling EfficientServer mod was
removed in 2.5.0; APM measures and never edits optimizer config).

### Web authorization matrix

| Endpoint | Verbs | Required level | Notes |
|---|---|---|---|
| `/api/apm` | GET | 0 (admin) | read-only telemetry snapshot |
| `/api/apm` | POST/PUT/DELETE | 0 + not implemented | base handler answers 405 |

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
the version-pinned `npx` TypeScript path inside `make bridge-build`; do not
hand-edit the generated bundle. Node.js/npm is required, but no global `tsc`
installation is needed.

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
mod version 2.4.1.

```bash
make bridge-build
make bridge-install
```

Restart the dedicated server, then run `apm capabilities`, `apm status`, or
`apm dump`. JSON is written under
`Mods/7dtd-server-apm-bridge/telemetry/` using `7dtd.apm.app.v3`.

Hooks are resolved by type and method name at startup. Missing hooks are marked
`unavailable` and do not prevent other instrumentation from loading. Deep AI and
path hooks are disabled by default because high-frequency timing has measurable
overhead. This mod requires EAC to be disabled and must be rebuilt/revalidated
after game updates.
