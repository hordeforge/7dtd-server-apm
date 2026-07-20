# Optional in-game instrumentation bridge

`7dtd-apm-bridge.dll` is an instrumentation-only 7DTD server mod. Host-only
capture remains supported without it. The bridge adds managed subsystem timings,
world/runtime gauges, spike records, capability reporting, periodic atomic JSON,
and the `apm` console/telnet command.

It is also a native V3 WebDashboard plugin. `WebMod/` adds a **7DTD APM**
settings panel and authenticated `GET /api/apm` exposes the same bounded
snapshot used by console capture. The endpoint defaults to administrator
permission level 0. Enable `WebDashboardEnabled`, browse to its configured port
(8080 in the loadgen profile), and sign in normally; the mod opens no separate
web listener.

Map delivery telemetry separates `ChunkManager.SendChunksToClients`, chunk and
map serialization, initial world-folder transfer, connection serialization, and
send-queue flushing. The snapshot and dashboard also expose per-package counts,
total bytes, and last/maximum package size under `mapTransfers`.

The `gc` block reports window-scoped Boehm collections, heap delta, and a gross
allocation counter (`grossAllocBytesPerSecond`) via `GC.GetTotalAllocatedBytes`
where the runtime provides it (Unity 2022 Mono does not, reporting `-1`; the
host `mono_alloc` probe supplies gross allocation there instead). Deep hooks
include tile-entity chunk load (`TileEntity.InstantiateFromRead`,
`TileEntityFeatureData.InstantiateModule`) so serialization cost is measurable
alongside the allocation churn it drives. Current schema `7dtd.apm.app.v3`,
mod version 2.1.0.

```bash
make bridge-build
make bridge-install
```

Restart the dedicated server, then run `apm capabilities`, `apm status`, or
`apm dump`. JSON is written under
`Mods/7dtd-apm-bridge/telemetry/` using `7dtd.apm.app.v3`.

Hooks are resolved by type and method name at startup. Missing hooks are marked
`unavailable` and do not prevent other instrumentation from loading. Deep AI and
path hooks are disabled by default because high-frequency timing has measurable
overhead. This mod requires EAC to be disabled and must be rebuilt/revalidated
after game updates.
