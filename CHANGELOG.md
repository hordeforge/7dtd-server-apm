# Changelog

User-facing changes for the two shipped artifacts. They version independently:

| Artifact | Version source | Distributed via |
|---|---|---|
| `seven-dtd-apm` host CLI | `pyproject.toml` = `tools/apm_suite/__init__.py` (gated by `scripts/check_version.py`) | local `uv sync`; printed by `uv run 7dtd-server-apm --version` |
| `7dtd-server-apm-bridge` server mod | `ModInfo.xml` = `BridgeMod.cs` const = `bridge/README.md` claim (same gate) | zip from `make package`, named after the newest git tag |

Git tags `vX.Y.Z` mirror the **bridge** version and carry annotated release
notes (`git show v2.3.0`); `.github/workflows/release.yml` rejects a tag that
disagrees with `ModInfo.xml`. Only the bridge is tagged, so a CLI-only release
ships without one. Breaking telemetry-schema or config changes to the bridge
are expected to bump its major version.

The **host CLI** has no tag and no external SemVer policy was ever written
down, so the rules below are inferred from the released sections here, not
adopted from a published policy. Its public contract is the `7dtd-server-apm`
command surface and the on-disk session, manifest, and budget schemas
(`7dtd.apm.budget.v2`):

- **patch**: a fix that leaves every command, flag default, and schema field
  readable exactly as before.
- **minor**: a new command, flag, or output field.
- **major**: a removed or renamed command or flag, a changed flag default, or
  a session/budget/manifest field that a current reader cannot parse.

A reader that has to special-case a new field to keep working is a major, not
a minor. Bump `pyproject.toml` and `tools/apm_suite/__init__.py` together and
add the released section here; `scripts/check_version.py` fails the build
when either the two files or this changelog disagree with the shipped version.

`v2.2.4` is tagged at a commit whose `ModInfo.xml` still read 2.2.3; it
predates the tag gate and no 2.2.4 mod was ever built. The bridge goes 2.2.3
to 2.3.0 and 2.2.4 stays skipped.

## Unreleased - host CLI

- Export: `app/efficientserver_log_excerpt.txt` now stays out of a support
  bundle. The exclusion list named it in `docs/APM.md` but the code excluded
  `FINALIZE.txt`, which nothing in the repository has ever produced, so an
  operator-attached slice of the same server log as `app/bridge.jsonl` was
  scrubbed and shipped. Its section timings survive in `csharp_bridge.json`.
- Export: a `meta.json` `utc` the session cannot spell no longer aborts the
  export with a bare `ValueError` traceback after the evidence has already
  been written into the bundle. The bundle manifest falls back the same way
  the audit does, and the malformed value is still reported.
- Correctness: the UTF-8 stdout/stderr pin actually runs again. Typer keeps a
  single registered callback slot, and a second `@app.callback()` overwrote
  the one that called `force_utf8_stdio`, so every command ran under the
  process locale despite `tools/README.md` promising the pin. The pin moved
  into the `root` callback and a test now fails if it is removed or if a
  second callback shadows it.
- Supply chain: the version-pinning guard for executed JS toolchain calls
  covers `bunx` as well as `npx`. The build, `lint-webui.sh`, and `lint-html.sh`
  all fetch and run through `bunx`, so guarding `npx` alone left the path
  actually used uncovered; the guard now matches both runners.
- Audit: `manifest.json` window stamps now describe the capture instead of the
  machine that read it. `started_at`/`ended_at` come from `meta.json`
  (`utc`, `utc + seconds`), and an export bundle carries the recorded stamps
  over unchanged, so auditing or bundling the same session twice writes the same
  bytes. A `meta.json` with no usable `utc` records `started_at: null` plus a
  warning; it no longer stamps the auditing host's wall clock as the capture
  start.
- Packaging: the sdist now has an explicit include list. Hatchling's default
  shipped the whole checkout (docs, plans, bridge sources, shell collectors);
  the sdist exists to build the wheel, which carries `apm_suite` alone.
- Packaging: `[project.urls]` gained `Homepage`, `Issues`, and `Changelog`.
- Packaging: `make bridge-install` now prunes shipped files that the new build
  no longer produces, so a dropped or renamed WebMod asset cannot linger in
  `Mods/` and keep being served. `Config/` is never pruned.
- Packaging: `make bridge-uninstall` moves the tuned
  `Config/apmbridge.json` to `Mods/7dtd-server-apm-bridge-config.json` before
  removing the mod folder, instead of deleting settings that the release zip
  only ships as a `.example`. A second uninstall no longer overwrites that
  saved file: the reinstall seeds a factory config, so the moved copy takes the
  next free `.1`, `.2`, name.
- Tooling: the lint-webui vendored plugin cache is extracted into a staging
  directory and renamed into place, so an interrupted extraction no longer
  leaves a half-populated `anti-slop-<sha>` that every later run accepts as a
  populated cache and never retries.
- Tooling: that cache directory is keyed by `ANTI_SLOP_SHA`. It was keyed by
  name alone, so bumping the pin kept serving the previous commit's rules and
  skipped the `ANTI_SLOP_SHA256` check on every later run.
- Tooling: the folded-stack tag memo is bounded (`lru_cache`, 65536 entries).
  A full-mode perf map contributes hundreds of thousands of distinct frame
  names, so the old unbounded dict grew with the input for the whole pass.
- Validity: a window whose app scrape only holds failed telnet records is
  `app_sim unavailable` instead of a collected layer with no evidence. The
  collector result, the summary state, and `audit` all name the cause.
- Audit: an unreadable or vanished session document (`meta.json`,
  `summary.json`, `health.json`, events, collector results, recorded manifest)
  is reported as an audit error instead of raising out of the audit.
- `export`: a hand-mangled `meta.json` timestamp no longer aborts the bundle
  with a bare `ValueError`; the exported manifest falls back to the session's
  own tolerant UTC parsing.
- `export`: an operator-attached slice of the server log stays out of the
  bundle. Exclusion is by what a file holds, not by extension, so
  `app/efficientserver_log_excerpt.txt` (and any other `efficientserver*` file
  in the session) is dropped for the same reason `app/bridge.jsonl` is. Its
  section timings survive in `csharp_bridge.json`.
- `scenario run`: an unattributable loadgen stats file is reported and the
  session is still audited, instead of raising after a successful capture.
- Text encoding: stdout and stderr are now pinned to UTF-8 for every command
  and every helper under `tools/host_profiler/`, so a `LANG=C` environment (a
  bare systemd unit, cron, `sudo` without `-E`) no longer turns the first
  non-ASCII character a report prints into a `UnicodeEncodeError` traceback
  where the report should be. A session path under a non-ASCII home directory
  or a hostname quoted inside an OS error was enough to trigger it.
- Text encoding: `stackcollapse_perf.py` reads a piped `perf script` as UTF-8
  with undecodable bytes replaced, the same policy it already applied to the
  file path, and writes UTF-8. `sys.stdin` had been using the locale encoding
  with `surrogateescape`, so the same bytes took a different path depending on
  how they arrived, and a non-ASCII frame name lost the whole `stacks.folded`.
- Text encoding: `doctor --json -` emits UTF-8 bytes directly, matching the
  encoding of the same report written with `--json <file>`.
- Packaging: the build backend is now bounded to one major
  (`hatchling>=1.27,<2`). It is the one dependency `uv.lock` cannot hash-pin,
  because PEP 517 build isolation resolves it outside the lock on every
  `uv sync` of the editable install.
- Build: every pinned tool version (tsc, the oxlint stack, the anti-slop
  commit and its sha256, vnu) now lives in `scripts/lib/tool_versions.sh`, so
  the release build and the lint gates cannot drift to different compilers.
- Build: `global.json` pins the .NET SDK to `8.0.423` with `rollForward:
  latestPatch`. `latestFeature` accepted any later 8.0 SDK a host happened to
  have, so the shipped DLL's compiler was not the one the repo declared.
- Build: `scripts/build_bridge.sh` pins `LC_ALL`/`TZ` and derives
  `SOURCE_DATE_EPOCH` from the HEAD commit (overridable), matching what
  `scripts/package.sh` already did for the archive.
- Release: the release zip normalizes member mode bits (0644 files, 0755
  directories) before archiving, so a package built under a restrictive umask
  is byte-identical to one built under 022, and a missing `zip` fails with a
  named tool instead of a mid-rule error.
- Release: `make package` now writes `dist/sbom-python.txt` and
  `dist/sbom-python.cdx.json` next to the zip, so every release carries the
  production dependency inventory (name, version, artifact hashes, CycloneDX
  graph) a scanner or downstream consumer can read. The inventory stays out of
  the archive: it unzips into `<server>/Mods/`.
- Config: the readers of the bridge's `Config/apmbridge.json` (`doctor`,
  `monitor`) now parse the commented example the mod itself accepts, so a
  freshly installed config is not reported as unreadable. `monitor` also
  treats a non-object config and a boolean `PeriodicExportSeconds` as the
  documented default instead of raising out of the sample loop, and holds the
  stale-read threshold to the bridge's own upper bound.
- Config: `find_server.sh` exits 1 when `SEVENDTD_DS_BIN` is set and no
  running server resolves to it, instead of falling through to the truncated
  process-name match and handing back another install's PID. The lenient
  fallback (with a warning) stays for the derived default.
- Generated pages (report, dashboard, session index, flame delta) now share
  one token set and one type scale from `tools/apm_suite/web_tokens.py`
  instead of four inlined copies of the palette that had drifted: the
  dashboard alone carried a card radius and a 14px body while the other three
  fell back to the 16px browser default. The dashboard's rounded cards become
  a 2-up tile row for summaries and rule-separated full-width blocks for
  tables, numeric cells are right-aligned with tabular figures, and the
  session index's artifact emoji are now words.
- Budget gate: a `max_layer_scores` or `max_sum_layer_score` limit that is not
  a number reports UNKNOWN instead of raising `ValueError` out of the whole
  gate. Both paths converted the limit with `float()` before `gate()` could
  read it, so one hand-edited budget field took the pass/fail verdict down
  with it.

- `verify-store [STORE]`: read-only integrity audit of every session in a
  session store, so a whole-store copy-back can be proven instead of assumed.
  Reports `ok` / `incomplete` (no recorded manifest, or required documents
  still missing) / `INVALID` (hash drift, schema failure, escaping recorded
  path) per session, exits non-zero on `INVALID` (`--strict` also on
  `incomplete`), and writes nothing. `audit` cannot serve this role: it
  re-stamps `manifest.json` on a clean session, so on a restored copy it would
  absorb the drift it is meant to detect.
- Correctness: a session document or budget file that carried a scalar or
  list where an object is expected (`{"metadata": 5}`, `"layers": 5`,
  `{"meta": 5}`) raised out of the store index, the budget gate, session
  comparison, and the report's layer scoring. `x.get(key) or {}` only defends
  a missing key, never a wrongly typed one. All four readers now coerce with
  `models.as_mapping` and read such evidence as absent, matching how
  `as_number` already treats unparseable scalars. Budget limits go through
  `as_number` too: a non-numeric limit is now an `UNKNOWN` line (the gate
  fails closed) instead of a `ValueError` traceback.
- Correctness: a torn or hand-mangled `meta.json` in an imported bundle no
  longer fails the whole report; the host-side layer scores stay computable
  without it.
- Tests: deterministic fuzz targets for the store index scan and HTML render
  (determinism, finite pressure sums, no unescaped markup, `index.json`
  round trip) and for the budget gate (UNKNOWN on unparseable limits and
  evidence, never a pass it could not decide), plus regression cases for
  every wrongly typed container shape above.
- An interrupted capture (`capture`, Ctrl-C) records `observed_seconds` in
  `meta.json` next to the `seconds` it requested. Rates (futex stalls/s, net
  MB/s) and the `compare` duration gate now read the window that actually ran,
  so a capture cut short no longer passes as a full-length one and its
  understated rates no longer read as a real regression.
- `meta.json` `layers` is derived from the collector catalog for the requested
  `--only` plan instead of a fixed list, and `capture_preset` holds the preset
  name (`standard` / `deep` / `forensic`, from `scenario run`) instead of the
  expanded `--only` string it duplicates in `only`.
- One `manifest.json` write per capture: `finalize`'s manifest stage already
  stamped the session, and `capture` no longer re-audits it, which halved the
  SHA-256 work over every collected artifact at the end of a run.

## Unreleased - bridge mod

No pending changes.

## 3.0.0 - bridge mod - 2026-09-28

Major bump for one breaking config change; every other entry leaves the
snapshot contract readable as before, so per the rule above they do not
force a schema bump.

- Config (breaking, 3.0.0): `Config/apmbridge.json` no longer accepts an
  unknown key. A misspelled `DeepMode` used to load as the default and leave
  the mod reporting the sections it was asked to change as unavailable, with
  nothing in the log; the file is now rejected, the reason is logged, and the
  mod runs built-in defaults. A config carrying a key this version no longer
  reads (or one from a future version) must be corrected before it takes
  effect.
- Config: the startup line and `apm reload` now log the config file that was
  read (or "built-in defaults") and the values in force after clamping, so the
  active config is readable from the server log. `apm reload` keeps the
  settings already in force when a re-read is rejected.
- Config: every default and clamp bound is a named constant in
  `BridgeConfig.cs`, and `bridge/README.md` documents each key, its type,
  default, and accepted range. The shipped `apmbridge.json.example` is
  commented; the example had no description of what any key did.
- API contract: `bridge/README.md` now documents the `GET /api/apm` response
  itself (status codes, the `SNAPSHOT_FAILED` error envelope, every top-level
  key, and which fields are nullable), not just the authorization matrix.
- API contract: `GET /api/apm` now caps `spikes` at the newest
  `Telemetry.DashboardSpikeRecords` (12) records instead of carrying the full
  128-entry ring, so a long spike streak no longer pushes 128 world samples
  down the wire on every 2 s poll. Before: a client paging the array for spike
  history read the whole ring. After: it reads the newest 12, and must not
  read a shorter array as "that was all of them". Element shape, order
  (newest last), and key names are unchanged, so by the rule in
  `bridge/README.md` this is not a `schema` bump. The periodic JSON export
  file is unaffected and still carries the full ring for audit and compare.
- API contract: `world.utc` no longer carries the string `"unavailable"`
  before the first world sample; it is `null`, like every other unmeasured
  value in the payload. No documented consumer read the old literal, and a
  client parsing `utc` fields as timestamps no longer has to special-case a
  date slot holding a placeholder. Every `utc` field in the snapshot is an
  ISO-8601 instant or absent.
- Correctness: an unrecognized `apm` console verb now answers with the
  verb list instead of silently returning the `status` summary, so a typo no
  longer looks like a successful call. `apm benchmark <non-number>` reports
  the bad argument instead of quietly benchmarking the default iteration
  count, and `apm jitmap FULL` matches its case-insensitive verb.
- Correctness: the GC window baseline is now captured on first read, not only
  by `apm reset`. On a server that was never reset (the default:
  `--reset-bridge` is off) the heap and collection bases stayed at 0, so
  `heapDeltaBytes` was the entire live heap and `windowSeconds` the process
  uptime. The host divides one by the other and reported the live heap as
  ~20 MB/s of net heap growth for a window that grew by a few MB. The window
  now starts when the bridge loads; `apm reset` still re-baselines as before.
- Correctness: the export deadline now rounds up to the next whole
  `Stopwatch` tick instead of truncating toward zero, so a positive
  `PeriodicExportSeconds` under one tick no longer arms a deadline in the past
  and exports on every frame.
- Correctness: the export and GC window deadlines are now scheduled on the
  monotonic `Stopwatch` rather than cached `Time.realtimeSinceStartup` values.
  `realtimeSinceStartup` is a float whose resolution degrades to 2 s once the
  process has been up about 194 days and to 4 s past about 388, which
  quantized a 30 s export window to 28 s or 32 s and let `windowSeconds`, the
  denominator of every reported rate, drift with it.
- Correctness: a frame timestamp read and the trash grace clock are now taken
  under their guards, so a concurrent spike sample or a prune sweep cannot
  race the main thread into a torn read.
- Correctness: `apm jitmap` releases its host claims and OS handles on every
  exit path, including a failed capture, instead of leaking the bind mount
  and file handles for the life of the process.
- Dashboard: the WebMod panel builds one layout pass shared by the spike table
  and the session report and sends fewer bytes per response, so a long
  capture no longer stutters the dashboard or floods the browser with payload
  it immediately discards.

## 2.2.0 - host CLI - 2026-08-26

First CLI version bump since the initial drop. Everything below accumulated
against 2.1.0.

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
- Fixed: an out-of-range integer `t` in any collector record (JSON integers are
  unbounded; `10**400` parses fine) raised `OverflowError` out of `float()` and
  aborted the whole required events stage, losing the timeline for the session.
  Timestamp coercion now goes through the same `as_number` helper every other
  unvalidated field uses, so a corrupt stamp reads as untimed.
- Fixed: `EventV2.source` is a declared field instead of a pydantic extra. The
  per-source retention cap (`PER_SOURCE_MAX`) is built on it, so readers and the
  type checker now both see it.
- Gates: `make lint`, `make typecheck`, and `make test` all failed on the
  committed tree. Ruff had no `src` setting, so it classified `apm_suite` as
  third-party and demanded contradictory import orders in different test files;
  mypy reported six errors (a dead `isinstance` guard typed away, a fuzz
  generator annotated as narrower than it is, the undeclared `source`); and two
  fuzz targets failed, one on the `OverflowError` above and one on a regression
  fixture that fed a list-rooted `apm_app.json` while asserting spikes came out
  of it. The fixture now covers both shapes separately: a list root is foreign
  evidence and reads as absent, an object root still yields its spikes.
- Repository: scratch directories in `check_bt.sh`, `lint-html.sh`,
  `lint-webui.sh`, and `package.sh` are created under the repo's gitignored
  `.scratch/` instead of `/tmp`, which is tmpfs on typical hosts (the staged
  release tree and the compiled webmod output were being held in RAM).
- Repository: the checkout root is found by walking up for the
  (`pyproject.toml`, `tools/apm_suite`) marker pair rather than by counting
  parent directories, in `apm_suite.paths` and in the `check_version.py` gate,
  which cannot import the package whose version it checks. Four test modules and
  `plans/scale_ladder.py` now take the one definition instead of recomputing it;
  `proc_sample.py` resolves `find_server.sh` as its own sibling and no longer
  swallows every exception from that lookup.

## 2.5.0 (tag v2.5.0) - bridge mod - 2026-09-21

Feature-removal release (minor bump): the cuts come from the workspace audit;
everything removed was either outside this repository's boundary or a second
copy of something that already exists.

- Removed: the `/api/perf` admin switch. `POST /api/perf` let a dashboard
  admin edit the sibling EfficientServer config and schedule a console
  `shutdown`; that is a workspace-boundary violation (APM measures, it never
  writes optimizer config) and the subject of THREAT_MODEL R1, now closed.
  Gone: the `Perf` REST class, the `PerfModConfigPath` bridge config knob,
  the dashboard's Efficiency panel with its feature-group toggles, their
  styles, and the two contract tests pinning the endpoint's behavior.
- Removed: `tools/apm/capture.sh`, an argv-forwarding wrapper around the
  Python CLI that re-typed a subset of the capture flags and drifted from
  the real surface. Use `uv run 7dtd-server-apm capture`.
- Removed: `tools/host_profiler/flamegraph.py` and the static `flame.svg`
  output. The interactive `flame.html` and the speedscope profiles cover the
  same folded stacks with search and zoom; the summary `flames.svg` link is
  gone with it.
- Changed: `telnet_command` is now a thin wrapper over the shared telnet
  session (`_telnet_session`), which `telnet_exec` also uses; the two had
  drifted into different timeout and drain behavior.
- Changed: the CLI `prune` command and post-capture auto-prune walk one
  shared `session.prune_store` pass instead of two copies of the same
  three-phase loop.
- Changed: `runner.run` no longer carries an argv password-redaction loop;
  repo policy forbids passwords in child-process argv and no call site
  passed one.

## 2.4.1 (tag v2.4.1) - bridge mod - 2026-09-20

No functional change. The bridge carries the version bump so the release is
taggable; what moved is tooling upkeep: the host CLI dev dependencies refresh
pydantic, typer, and ruff patch releases via dependabot. Tagged because the
release convention tags bridge versions, keeping the tag series unbroken.

## 2.4.0 (tag v2.4.0) - bridge mod - 2026-09-11

No functional change. The bridge carries the version bump so the release is
taggable; what moved is documentation: the research citations now point at the
grouped `docs/<subsystem>/` tree, and `AGENTS.md` states what this repository
owns and does not own.

## 2.3.0 (tag v2.3.0) - bridge mod - 2026-08-26

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
