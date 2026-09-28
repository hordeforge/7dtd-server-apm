# Changelog

User-facing changes for the two shipped artifacts. They version independently:

| Artifact | Version source | Distributed via |
|---|---|---|
| `seven-dtd-apm` host CLI | `pyproject.toml` = `tools/apm_suite/__init__.py` = `uv.lock` (gated by `scripts/check_version.py`) | local `uv sync`; printed by `uv run 7dtd-server-apm --version` |
| `7dtd-server-apm-bridge` server mod | `ModInfo.xml` = `BridgeMod.cs` const = `bridge/README.md` claim (same gate) | zip from `make package`, named `vX.Y.Z` on a clean-tag build and `<commit>` otherwise |

Git tags `vX.Y.Z` mirror the **bridge** version and carry annotated release
notes (`git show v2.3.0`); `.github/workflows/release.yml` rejects a tag that
disagrees with `ModInfo.xml`. A released bridge section with no
`(tag vX.Y.Z)` suffix is not released yet: the tag is what publishes it, and
an untagged tree packages under its commit id, so a consumer following the
changelog alone can be ahead of what exists. Only the bridge is tagged, so a
CLI-only release ships without one. Breaking telemetry-schema or config
changes to the bridge are expected to bump its major version.

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
a minor. Bump `pyproject.toml` and `tools/apm_suite/__init__.py` together, run
`uv lock` (the lock records the project version, and every `make` target runs
`uv run --locked`), and add the released section here;
`scripts/check_version.py` fails the build when any of the three files or this
changelog disagree with the shipped version.

`v2.2.4` is tagged at a commit whose `ModInfo.xml` still read 2.2.3; it
predates the tag gate and no 2.2.4 mod was ever built, so 2.2.4 stays skipped
and the next shipped bridge after 2.2.3 was 2.3.0.

## Unreleased - host CLI

- Import: the restored session is audited against the manifest the bundle
  carries before a new one is written, and the findings are printed one per
  line. A bundle that drifted since the export is now reported by name instead
  of re-stamped: the plain audit rewrites `manifest.json` unconditionally, so
  a tampered member was absorbed by the very command meant to restore it, and
  the restored session was left with no baseline to re-check against.
- Events: the per-source retention bound keeps the most severe events, recency
  breaking the tie, instead of the first ones parsed. The bound decides what
  reaches the global one, so a source that flooded routine warnings before
  reporting the error that ended the capture had exactly that error discarded.
- Compare: the collector-selection gate matches the `--only` tokens as a set,
  so `cpu,io` and `io, cpu` are the same plan. The gate is about whether both
  sides collected the same evidence, and the collector catalog resolves tokens
  to a set; a genuine mismatch is still rejected.
- Audit: `manifest.json` reports the window the collectors actually ran. A
  capture cut short still records the seconds it asked for, so the end stamp
  named a time nobody measured, on the same rule `compare` already gates on.

- Export: server console lines are dropped from every text member by content,
  not only files whose name the exclusion list happens to know. An operator who
  drops a console capture or a chat log into a session under a neutral name
  (`console_capture.log`) shipped a bundle carrying player names, connect IPs,
  and Steam IDs; the line is recognized by the ISO-8601 timestamp the game
  stamps on it, the same shape `app_scrape.py` already cuts at on the telnet
  wire. JSONL members are untouched: a record there is the tool's own
  structured telemetry, scrubbed field by field.
- Export: `app/bridge.jsonl` is created 0600 even when `app_scrape.py` runs
  standalone against a caller-chosen `--out`; a capture session was already
  0700, so this only closes the non-capture invocation.
- Capture: a collector result that cannot be written (full disk, unwritable
  session directory) is warned into `WARN.txt` and the launch loop continues.
  The loop records a result for every skipped and unavailable collector, so the
  write used to abort the loop and lose every collector that had not started
  yet, before finalize or the audit ran.
- Capture: the launch loop and the per-collector classification now take one
  stat to decide an artifact is a regular file with content, so an entry
  removed by a concurrent prune reads as no evidence instead of raising.
- Prometheus: an unreadable `health.json` or `csharp_bridge.json` is reported
  by its own path, as the `summary.json` read already was. A read failure
  reached the command's output-write handler and blamed `--output` for a
  destination that was never touched.
- Scenario: a loadgen manifest that cannot be statted, read, or written is
  reported and the attach is skipped, matching the stats copy above it. The
  capture and its evidence were already on disk; a manifest that lost the race
  with a concurrent store cleanup no longer aborts the audit and the scenario
  exit code with a traceback.
- Report: a summary, health, events, or bridge document that cannot be read is
  named on stderr while the report renders without it. The page is a summary,
  not a source of truth, so a malformed document still degrades rather than
  failing the render stage; it just no longer degrades silently.
- Doctor: a `sudo` that cannot be launched (lost `+x`, unreadable interpreter)
  reports as a failed check with its own reason instead of aborting the whole
  report on an unhandled `OSError`.
- Collectors: `proc_sample.py` bounds its `find_server.sh` pid lookup, so a
  wedged locator is reported as a missing pid instead of hanging the collector
  before its first sample.
- Backup: new `7dtd-server-apm backup DESTINATION` command copies the session
  store off-host and proves the copy. The store is the tool's only durable
  state and nothing copied it, so a host loss took every session with it. Each
  session is copied through a staging rename (an interrupted run leaves whole
  sessions or none), a rerun skips sessions whose recorded manifest hash is
  unchanged, `.scenario` and the index come along, and `.trash` deliberately
  does not. Sessions still capturing are named as skipped, a run that backs up
  nothing exits 1, and the destination is audited against its recorded hashes
  on every run (including sessions an earlier run copied) with exit 1 on any
  `INVALID`. Pruning the live store no longer shrinks the archive. A
  destination inside the store, or a store inside the destination, is refused;
  a destination on the same filesystem is reported as a warning rather than
  an error, since staging before an upload is a legitimate use. The
  destination can be given as an argument or through the new
  `SEVENDTD_APM_BACKUP_DIR`, which `doctor` reads too: its `store_backup`
  check fails when nothing is configured, nothing has been copied, or the last
  run recorded no session, and carries the age of the last run.

- Events: past the 2000-event retention bound the timeline keeps the most
  severe events, newest first within a severity, instead of the first ones
  parsed. A busy window emits more than the bound and the first events are
  routine samples from the opening seconds, so a long capture's ending stall
  used to be dropped behind thousands of quiet records. The survivors are
  still laid out chronologically, and `by_kind` still counts every event.
- Sessions: a record block that is not a list, or a list holding non-objects,
  now reads as absent evidence across every unvalidated reader
  (`prometheus`, `compare`, `scaling`, `index`, the bridge section parser, the
  event timeline, and the thread summary) instead of raising `AttributeError`
  mid-analysis. One shared `object_list` coercion joins the `as_number` and
  `as_mapping` family in `models.py`; no schema field changed, so the emitted
  documents and the gates read the same values as before.
- Scaling: the `scaling` document now records that its `total_exponent` column
  is only a fit on bridge-reset windows, the caveat `compare` already carried
  for subsystem attribution. `totalMs` is cumulative since the last `apm reset`.
- Events: `EventV2` declares the `t`, `value`, and `line` fields the readers
  already consumed as untyped extras, so the type checker sees them. The
  timeline now writes them as explicit nulls when an event has none, and
  `EventSink` normalizes them through the shared numeric coercion, so an
  out-of-range stamp from a collector drops the field instead of failing
  validation and taking the events stage down with it.
- Fix: an untrusted session document that is not UTF-8, nested thousands of
  levels deep, or too deep for the interpreter recursion budget no longer
  escapes as a `UnicodeDecodeError` or `RecursionError` traceback out of
  stages meant to degrade to absent evidence. `io.read_text` and
  `io.json_loads` turn both into the `ValueError` contract every reader
  already guards, and `json_loads` rejects a document nested past
  `MAX_JSON_DEPTH` at the boundary (the recursive surrogate scrub and every
  writer behind it would each fail on it later anyway). Every untrusted
  `json.loads` site in `io`, `analysis/`, `bundle`, `capture`, `cli`, and
  `finalize` routes through them.
- Fix: a scalar or list where an object belongs in an imported session
  document no longer raises `AttributeError` out of a verdict stage.
  `analysis/scaling` reads a scalar `metadata`/`world` and a scalar entry in
  `top_managed_sections`, `analysis/bridge` reads a scalar entry in
  `top_managed_sections` and in `layers` (and a non-object `signals` block),
  and `compare` reads a non-object `workload.json`, as absent evidence.
- Tests: fuzz targets for the scale-ladder fit, the `compare` gate, and the
  untrusted JSON decode path (depth, encoding, surrogates, torn JSONL), plus
  a regression case per crash above.
- Tooling: a `.pre-commit-config.yaml` runs ruff, `ruff format --check`, mypy,
  and shellcheck at commit time, and the WebMod, HTML, and pytest gates at
  push time. Every hook calls the same `make` target CI runs, so the local and
  remote rule sets cannot drift.
- Tooling: ruff gained `SLOT`, `LOG`, `BLE`, and `ERA`. The only findings were
  `finalize`'s stage wrapper, which catches every exception on purpose and
  now says so on the line, and a section-nesting comment in
  `analysis/bridge.py` that read as commented-out code and is now respelled as
  prose.
- Tooling: mypy gained `mutable-override`, `narrowed-type-not-subtype`, and
  `unused-awaitable`, all clean across `tools/`, `scripts/`, and `plans/`.
- Tooling: the repo-level gates that assert on the bridge sources, the
  packaging config, and the CI pins moved out of `tools/apm_suite/tests/`
  into `tests/`. They never exercised `apm_suite` code; they sat in the
  package's test directory only because `testpaths` pointed there. `pytest`,
  `ruff`, and mypy cover both roots.
- Tooling: `apm_suite.session.mtime_or_zero` replaces the private `_mtime`
  helper, which `plans/scale_ladder.py` had to copy because the original was
  not importable. The ladder now calls the one implementation.
- Evidence integrity: the scale ladder attaches its `workload.json` to the
  session named by the sessions that appeared while its capture ran, and
  attaches nothing when that is not exactly one. A mtime window cannot prove
  ownership: a concurrent or scheduled capture, or a re-run of the ladder,
  lands in the same window, and the newest-session pick wrote the manifest
  into a session the run did not create, breaking that session's recorded
  hashes and reporting it INVALID.
- Performance: `compare`'s flame-frame delta built and sorted one dict per
  unique frame to return the top 20, which on a mid-size session cost ~12 s of
  CPU and ~200 MB of garbage. It now selects with `heapq.nsmallest` over a
  generator; the names, the tiebreak, and the returned rows are unchanged.
- Performance: reading a session document scrubbed it for lone surrogates by
  rebuilding every nested list and dict, several times the cost of the parse
  itself on a multi-hundred-KB `summary.json`, and a store full of sessions is
  read once per session per `index` write. A lone surrogate can only enter a
  document through a `\uD800-\uDFFF` escape, so one scan of the raw text now
  decides whether the scrub has anything to find.
- Performance: the on-CPU ustack histogram was read into memory as text only
  to ask whether it carried more than a header. It is now a file-size check,
  and `summary` no longer holds a multi-megabyte copy of it for the build.
- Performance: `prune --max-bytes` walked the whole tree of every session the
  count policy had already doomed, only to subtract those bytes again. The
  budget freed is what the kept sessions occupy, so only they are measured.
- Performance: the integrity audit spent three stat calls per artifact
  (`is_file`, `is_symlink`, `stat`) and the required-document check two, for
  one answer each. Both are single `lstat`/`stat` calls now.
- Fix: `scenario matrix` stubbed `capture.telnet_command` in its test instead
  of the name `cli` calls, so the telnet-target routing the test asserts was
  never observed and the test failed. The stub follows every other CLI test.

## 2.3.0 - host CLI - 2026-09-28

Minor for the new `verify-store` command and the `index --store` option every
other store-taking command spells that way. No command or flag was removed and
no schema field was retyped, so per the rules above nothing here forces a
major.

- CLI: an unusable output path is a clean operator error, not a traceback.
  `doctor --json`, `export --output`, and `index` now exit 2 on a bad
  invocation (the path is a directory or unwritable) and 1 when the command
  ran and could not finish, through one `_fail` path that escapes the message.
  `index` also takes `--store`; `--root` stays as an alias, so a script
  written against either name keeps working.
- CLI: `--help` renders plain when stdout is not a terminal, so
  `7dtd-server-apm capture --help | grep -- --seconds` returns the option
  instead of box drawing and 100 columns of padding, and help pasted into an
  issue keeps its line breaks. Rich markup is still used on a terminal, where
  the decision is made once at import time.
- Security: a telnet reply line carrying server log text is cut at the first
  console-log timestamp instead of being kept whole when the timestamp was
  not at the start of the line. A stream write with no trailing newline lands
  the log line glued to the tail of the `apm` command reply, and the anchored
  match kept that line whole, player names and client addresses included. The
  reply text ahead of the first timestamp is what is kept.
- Security: `index` chmods the session store root to 0700 when it creates it.
  `index.json` carries every session's absolute path plus health and grade
  data, and `index` can be the first command a host runs, so this can be the
  call that creates the store. Same contract as `capture` and `import`.
- Security: capture refuses to replace a `/tmp/perf-<pid>.map` entry that is
  not a symlink owned by this uid, instead of unlinking whatever another local
  user planted there. perf would also have read a planted regular file as this
  capture's symbol table. The refusal is recorded as a session warning.
- Export: `app/efficientserver_log_excerpt.txt` now stays out of a support
  bundle. The exclusion list named it in `docs/APM.md` but the code excluded
  `FINALIZE.txt`, which nothing in the repository has ever produced, so an
  operator-attached slice of the same server log as `app/bridge.jsonl` was
  scrubbed and shipped. Exclusion is by what a file holds, not by extension, so
  any other `efficientserver*` file in the session is dropped for the same
  reason. Its section timings survive in `csharp_bridge.json`.
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
- Dependencies: the runtime and dev ranges no longer admit a version pair
  nobody gated. `psutil` and `types-psutil` both floor at 7, so mypy cannot
  type-check against stubs for a major the installed runtime is not, and
  `typer` is bounded at `<0.28` rather than `<1`: typer is pre-1.0, so a new
  minor now needs a `uv lock` and a suite run here instead of arriving with
  the next resolution. Resolved versions are unchanged.
- Packaging: the sdist now has an explicit include list. Hatchling's default
  shipped the whole checkout (docs, plans, bridge sources, shell collectors);
  the sdist exists to build the wheel, which carries `apm_suite` alone.
- Packaging: the sdist's top-level file patterns are anchored, so `README.md`
  no longer matches by name at any depth and pulls `tools/README.md`,
  `tools/apm/README.md`, `tools/host_profiler/README.md`, and
  `bridge/README.md` into the archive the wheel does not need.
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
- Release: `scripts/check_version.py` now reads the version `uv.lock` records
  for the root package and fails when it disagrees with `pyproject.toml`. A
  host CLI bump without `uv lock` used to fail every `Makefile` target with a
  message naming the lockfile, not the version that had to change.
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

- Console: a mistyped `apm` argument is refused instead of answered by a
  default. `apm jitmap FULLL` wrote the short map, `apm benchmark 10` was
  clamped up to the 1000-iteration floor, and a stray argument to a verb that
  takes none was dropped, so each answered like a successful call that measured
  something else. Every verb now declares the argument it accepts and the check
  runs before the dispatch; the verb list, the argument rules, and the help line
  are one table, so the usage a caller reads and the verbs the dispatcher
  accepts cannot drift apart. Valid calls are unaffected, and the rendered help
  line is unchanged.
- Console: `bridge/README.md` documents the verb set, its arguments, its
  replies, its refusal messages, and which verbs change server state, the way
  the REST response contract documents the endpoint. The verbs were listed only
  in prose scattered across the docs, so a caller scripting `apm` over telnet
  had no one place to read what exists.

- WebMod: the panel bundle is emitted with comments stripped, and
  `tests/test_bridge_build_surface.py` budgets the shipped `bundle.js` (36 KB)
  and `styling.css` (12 KB). The stock dashboard loads both on every page, so
  the emit dropped 10,581 B of source comments (44,099 B to 33,518 B raw,
  12,931 B to 8,930 B gzipped) that no browser renders. The freshness gate only
  compares a fresh `tsc` run, so it would not have caught the weight coming
  back.
- WebMod: the panel reads its colors from one token block instead of naming
  them in each rule. It renders inside the stock dashboard, so it cannot import
  `apm_suite.web_tokens`, but it can hold the same values: the six it uses are
  declared once as RGB channels (so a solid fill and its 20% pill tint come
  from one line) and every rule, plus the series and gauge colors in
  `bundle.ts`, names them. The top bars painted "within budget" in a different
  green from the level meters, three status hues carried a second hand-mixed
  translucent literal, and the gauge arc sat on a dark ink no other track in
  the panel uses, which vanishes on a light dashboard theme.
  `tests/test_bridge_build_surface.py` pins the block to `web_tokens.TOKENS`
  and fails on a raw literal in either file. `styling.css` goes 10,562 B to
  11,818 B, inside the 12 KB budget.
- Observability: `GET /api/apm` instruments itself. The panel polls it every
  2 s, so a failure there left the operator with a frozen dashboard and no
  record anywhere. The request is timed and counted as the
  `apm.api.snapshot` section, `health.apiRequests` and `health.apiErrors` carry
  the window totals, `apm status` prints them, and a failure logs the
  exception type, message, and stack trace instead of the message alone (the
  coded `SNAPSHOT_FAILED` response carries no detail).
- Observability: each unmeasurable field names its own failure in `health`
  (`lastExportError`, `lastSampleError`, `hostError`, `lastApiError`). They
  shared one slot before, so a successful export cleared a world-sample error
  another thread had just recorded and an unmeasured field read as healthy. A
  `null` `host` now names the `/proc` read that failed.
- Logging: the map-transfer counter and the `/proc` host read run per network
  package and per snapshot. A persistently failing one logged once per call
  (hundreds of lines per second during a join); both now log the first failure
  of a streak and stay quiet until the next success.
- API: `GET /api/apm` answers `Cache-Control: no-store`. The document is
  sampled when the request arrives rather than representing a URL, and the
  response carried no freshness of its own, so a caching proxy in front of the
  dashboard port could replay a stale snapshot as a current one. The payload,
  its status codes, and its `SNAPSHOT_FAILED` code are unchanged, so no client
  has to change.
- API: the response contract now documents `mapTransfers.mebiytes`, the MiB
  float beside `bytes` that the panel has always read.
- Evidence integrity: `apm dump` picks a name that is free. Two dumps inside
  one second (a retried capture, an operator repeating a command whose reply
  was lost) resolved to one second-resolution file, and the second publish
  replaced the first dump's evidence while reporting that path as written. The
  timestamp stays the name prefix, so the timestamped prune still keeps the
  32 newest dumps in chronological order.

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
