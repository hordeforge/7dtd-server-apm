# Changelog

User-facing changes for the two shipped artifacts. They version independently:

| Artifact | Version source | Distributed via |
|---|---|---|
| `seven-dtd-apm` host CLI | `pyproject.toml` = `tools/apm_suite/__init__.py` (gated by `scripts/check_version.py`) | local `uv sync`; printed by `uv run 7dtd-server-apm --version` |
| `7dtd-server-apm-bridge` server mod | `ModInfo.xml` = `BridgeMod.cs` const = `bridge/README.md` claim (same gate) | zip from `make package`, named after the newest git tag |

Git tags `vX.Y.Z` mirror the **bridge** version and carry annotated release
notes (`git show v2.2.3`). This de facto policy is inferred from history:
only the bridge has ever been tagged, and its in-file bumps precede each tag.
The CLI package has stayed at 2.1.0 since the initial commit despite ongoing
feature work; treat CLI minor bumps as pending until one lands. Breaking
telemetry-schema or config changes to the bridge are expected to bump its
major version.

## Unreleased

### Host CLI

- Correctness: `main_thread_share_of_process_avg` now divides the main
  thread's CPU by the whole-process CPU total the threads collector records
  per sample (`process_cpu_pct`), instead of by the sum of the truncated
  top-15 row list. On servers with more busy threads than the cap the old
  denominator inflated the share (a 35% main thread read as 53%), which could
  fire `main_thread_bound` and raise cpu layer pressure from a wrong value.
  Sessions captured by older collectors keep the legacy fallback.
- Resource lifecycle: a capture with `--symbolize` (and every `scenario run`,
  which symbolizes by default) no longer leaves its `/tmp/perf-<pid>.map`
  symlink behind. The name is published pre-window for perf's hardcoded
  lookup and now released in the capture teardown; removal only fires while
  the link still points at this capture's target, so an overlapping capture
  against the same pid keeps its own map. Stale links from earlier captures
  and dead server pids previously survived on tmpfs until reboot.
- Performance: the SVG flamegraph builder no longer slices a prefix tuple per
  stack depth (quadratic in stack depth) and renders without cyclic-GC passes;
  a 50k-line folded profile drops from ~28 s to ~3 s with byte-identical
  output. jitsym annotation and folded-stack annotation now stream their
  inputs instead of holding whole probe outputs resident, and finalize reads
  the forensic `mono_alloc` output once instead of twice.
- Performance: the remaining large-artifact readers stream line by line
  (flame weights/deltas, speedscope + SVG + interactive tree builds, folded
  hot-path ranking, events timeline, jitmap load, session compare), so
  finalize and compare no longer hold whole hundreds-of-MB folded stacks or
  tens-of-MB telnet scrapes resident on top of their results.
- Performance: the bridge hashes Assembly-CSharp.dll once per process for its
  identity block; the SHA256 was recomputed on every periodic export and every
  `apm dump` (default every 30 s, forever) for an immutable value.
- Supply chain: CI actions run from immutable commit SHAs instead of mutable
  tags (`actions/checkout` v4.4.0; `astral-sh/setup-uv` updated v6 to
  v10.0.1), Dependabot keeps `uv.lock` and those pins current weekly, and a
  guard test fails any future tag-pinned action or unpinned executed `npx`
  call in the scripts.
- Packaging: `make sbom` emits a hash-pinned production dependency inventory
  (`dist/sbom-python.txt`, name/version plus sha256 of every locked artifact)
  for releases and vulnerability scanners, plus a CycloneDX 1.5 BOM of the same
  locked resolution (`dist/sbom-python.cdx.json`, purl + dependency graph) that
  SBOM and vuln scanners ingest directly.
- Privacy: event timelines no longer embed raw telnet console text in spike
  messages; only the extracted `gmUpdateDuration` is kept. The console stream
  can carry player names, IPs, and Steam IDs.
- Privacy: export bundles scrub the host home prefix from `.jsonl`, bpftrace
  `.out`, and flamegraph `.svg` artifacts (previously copied verbatim), apply
  the `cmdline`/`exe` redaction to JSONL lines, and still exclude raw
  `bridge.jsonl` entirely.
- Privacy: the app scrape discards the telnet banner and post-logon reply and
  persists only the requested `apm` command responses. Streamed console-log
  lines interleaved into a command window (which can carry player names, IPs,
  and Steam IDs) are now dropped too: complete lines are matched by their
  timestamp prefix, split lines are rejoined across reads before matching, and
  an unclassifiable trailing fragment at socket close is discarded.
- Fixed: `audit` now honors its documented contract and verifies artifacts
  against the hashes recorded in `manifest.json` (it previously rebuilt the
  manifest from current contents, so edited evidence always passed). A failed
  verification preserves the recorded manifest, names the offending paths on
  stderr, and exits 1; newly attached files still verify clean.
- Fixed: folded/speedscope/flamegraph loaders crashed on non-finite sample
  weights (`int(inf)`); inf/nan samples are skipped now.
- Changed: `--only` token resolution is one shared rule (`models.collector_requested`
  over the collector catalog in `apm_suite/collectors.py`) across capture planning,
  summary scoring, and audit. Previously the plan and the audit used two different
  alias tables: `--only net` planned only `io_net` while the audit and summary
  treated it as the whole io layer (false "produced no usable evidence" warnings
  for vfs/block), and deliberately opt-in `mono_alloc` was flagged as missing
  evidence on every default capture. `--only net` now plans the full io layer;
  opt-in collectors answer only their own tokens.
- Fixed: budget, compare, and health scored collected layers through three
  drifting copies of the same logic; they share one helper now.
- Fixed: finalize-time lag diagnosis and the bridge analyzer apply identical
  deep-sample scaling (shared attribute helper).
- Fixed: session writes are now durable, not just atomic: the parent directory
  is fsynced after each rename, so a power loss can no longer revert evidence
  files to empty or missing after a reported-successful write.
- Changed: retention deletion is one shared implementation for `prune` and
  post-capture auto-prune; a single undeletable session (e.g. EBUSY from a
  leaked mono bind mount) no longer aborts a prune run and strands the rest.
- Changed: the seven per-analysis JSONL reader loops share one streaming
  reader (`io.iter_jsonl`), and `load_json` names the failing file on decode
  errors (compare/budget/bridge previously wrapped it identically in three
  places; a torn artifact now reports "cannot parse <path>" everywhere).
- Changed: the server process name prefix has one definition
  (`models.SERVER_COMM`) instead of five literals, and compare builds section
  and attribution deltas through one shared helper instead of two copies.
- Fixed: events.json ingestion rejects internally inconsistent documents
  (count must equal retained + dropped, retained must equal the number of
  materialized events) instead of feeding readers misleading totals.
- jitmap files with undecodable bytes no longer abort finalize; percentage
  shares are rounded instead of truncated.
- Packaging: the wheel carries complete metadata (MIT license expression,
  README, repository URL, author, classifiers) and no longer ships the test
  suite; capture and flamegraph commands fail with one clear message when run
  from an installed copy that lacks the collector backends instead of failing
  per collector.

### Bridge mod

- Changed: the stale-temp sweep and the atomic temp-to-final publish are one
  shared implementation (`TempFiles`) used by both the periodic telemetry
  export and jitmap publication, instead of two copies of each.
- Packaging: the release zip ships `Config/apmbridge.json.example` instead of
  the live config name, so upgrading by unzipping over `Mods/` no longer resets
  operator-tuned settings (`DeepMode`, `SpikeThresholdMs`, ...); `make
  bridge-install` seeds the live config from the example on first install only,
  and the mod runs on built-in defaults when no config file exists.
- Build: the TypeScript panel build is self-contained (pinned `npx`
  toolchain); no preinstalled global tsc setup needed.
- API: `POST /api/perf` counts effective changes only; a request that would
  change nothing now answers `changed: 0, restarting: false` and skips the
  config write and the server restart instead of kicking players for a no-op.
  A missing or unreadable perf config now answers `409 UNAVAILABLE` (matching
  GET's `available: false`) instead of a misleading `500 WRITE_FAILED`, which
  is reserved for real write failures. `GET /api/apm` answers a coded
  `500 SNAPSHOT_FAILED` envelope when snapshot serialization fails instead of
  an unhandled handler exception. Panel toggle buttons re-enable after a
  no-op response (previously stuck busy until reload).
- Packaging: the release zip no longer contains debug symbols (`.pdb`);
  `make bridge-install` replaces files by atomic rename so an upgrade cannot
  truncate a DLL the running server still has mapped, and reminds you to
  restart the server.

## 2.2.3 (tag v2.2.3) - bridge mod - 2026-08-22

Entries match the annotated tag message.

- WebUI overhaul: APM and Efficiency panels in TypeScript with live telemetry,
  perf-mod toggle, per-feature-group toggles with batch apply (one restart),
  descriptions and safe/experimental status badges, per-entry sidebar icons,
  auth-gated menu entries hidden while logged out, panel scroll fix, polling
  stops on auth failure.
- `/api/perf`: reports feature groups (description, status) and accepts
  top-level, per-group, and batch `{groups: {...}}` toggles.
- WebMod lint gate (tsc + oxlint) and W3C HTML/CSS validation gate.

## 2.2.2 - bridge mod - 2026-08-22

In-file version bump only; never tagged.

- Strict TypeScript fixes across the panel; perf panel CSS; freshness check so
  the committed `bundle.js` cannot go stale against `bundle.ts`.

## 2.2.0 - bridge mod - 2026-08-22

In-file version bump only; never tagged.

- Dashboard panel source moved to TypeScript (`WebMod/bundle.ts`) with a perf
  toggle; webui lint gate introduced.

## 2.1.0 - bridge mod

Reconstructed from the verification log (TODO.md R84/R90); the committed
ModInfo history jumps 2.0.0 -> 2.2.0, so this exact state predates the
consistency gate.

- Gross-allocation counter (`GC.GetTotalAllocatedBytes`; `-1` on Unity 2022
  Mono, where the host `mono_alloc` probe supplies gross allocation instead)
  plus tile-entity deep hooks (`TileEntity.InstantiateFromRead`,
  `TileEntityFeatureData.InstantiateModule`), letting serialization cost be
  measured next to the allocation churn it drives.

## 2.0.0 - initial public drop - 2026-07-20

- First commit of both artifacts: host CLI package 2.1.0 and bridge mod 2.0.0
  with telemetry schema `7dtd.apm.app.v3`.
