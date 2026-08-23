# Supported capabilities

**Owns:** APM product surface (what the CLI/suite does).  
**Not:** evidence model detail ([APM](APM.md)), bridge correlation ([APM_CS_BRIDGE](APM_CS_BRIDGE.md)), roadmap tickets ([ROADMAP](ROADMAP.md)).

The canonical product surface is the `7dtd-server-apm` CLI.

## Capability groups

| Group | Examples |
|---|---|
| Discovery | Process find, `doctor`, capabilities |
| Capture | Layered host collectors, bridge snapshots, `finalize` |
| Evidence | Coverage-aware health, event correlation, lag diagnosis |
| Profile | Flame build/diff, live `monitor`, Prometheus export |
| Compare | Compatible-session compare, regression budgets |
| Scale | Capture ladder, super-linear scaling detection |
| Ops | Sanitized export, retention prune, `index` |

Lag diagnosis covers GC gross-allocation churn, stop-the-world pause timing, allocation-site attribution, and kernel chunk bandwidth (detail: [APM](APM.md)).

## Explicit non-features

| Not APM | Owner |
|---|---|
| Generated LiteNetLib clients | Sibling `7dtd-loadgen` |
| Runtime Harmony optim patches | Sibling `7dtd-server-optimizer` |
| Historical optimizer load/profiler commands | Removed |
| Automatic Harmony patch generation | Removed (unsafe) |
| Raw eBPF/perf scripts as public API | Private collector backends |

Collector stderr, exit status, capture duration, and artifacts are represented in the session manifest. Optional collector failure preserves other usable evidence (layer becomes `unavailable`, never a healthy zero).

## Related docs

| Doc | Role |
|---|---|
| [APM](APM.md) | Capture / validity / lag diagnosis |
| [APM_CS_BRIDGE](APM_CS_BRIDGE.md) | Managed correlation |
| [COMPATIBILITY](COMPATIBILITY.md) | Supported matrix |
| [LOAD_PROFILE](LOAD_PROFILE.md) | Canonical heavy profile |
| [ROADMAP](ROADMAP.md) | Improvement backlog |
| Root README | [`../README.md`](../README.md) |

## Changelog

- **2026-07-19:** Ownership; capability table; non-features; related docs.
