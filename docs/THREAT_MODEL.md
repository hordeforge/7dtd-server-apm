# Threat model

Systemic view of what this repository's tooling can be attacked through, what
it costs, and which controls exist. Point vulnerabilities and fixes belong to
sec-review; this document is the map that aims those passes.

- **Scope:** host-only measurement CLI (`7dtd-server-apm`, `tools/apm_suite/`),
  shell and bpftrace collectors (`tools/apm/`, `tools/host_profiler/`), and the
  optional in-server bridge DLL (`bridge/ApmBridge/`). The game server itself,
  its stock WebDashboard implementation, and sibling projects
  (`7dtd-loadgen`, `7dtd-server-optimizer`) are outside this model; only the
  interfaces between them and this repo are modeled.
- **Last reviewed:** 2026-09-28, against commit `a3210b7`. Owner and review
  cadence: not assigned.
- **Disclosure path:** none documented. There is no SECURITY.md; until one
  exists there is no stated route from "vulnerability reported" to "fix
  shipped" (see Response readiness).

## Risk-ranked summary

| # | Risk | Boundary | Severity | Status |
|---|---|---|---|---|
| R1 | The telnet password is sent in cleartext to whatever answers `--telnet-port`, with no host identity check, so a rogue or redirected listener harvests full console control of the game server | B2 CLI -> telnet | Medium-High | Gap. Inherent to the stock plaintext telnet interface; not fixable in this repo. `capture.py:265-305`, `app_scrape.py:58` |
| R2 | Root-adjacent collectors driven by operator input: every capture shells out to `sudo -n bpftrace`, `perf`, and `mount --bind` against an operator-chosen `--pid`, so anyone who can invoke the CLI with passwordless sudo can profile arbitrary processes | B5 CLI -> root | Medium | Gap; no sudoers policy ships with the repo to constrain it |
| R3 | Session store leaks player PII (names, IPs, Steam IDs) if raw sessions leave the host | B3/B6 store -> other parties | Medium | Mitigated: owner-only perms on both captured and imported sessions, raw drain excluded from exports |
| R4 | Evidence integrity: a writable store lets a local attacker forge measurements that feed baseline/candidate verdicts | B6 store -> analysis | Low-Medium | Partially mitigated. Manifest-recorded artifact paths are validated at read, and hashes are checked at finalize/import, but nothing is signed, so a local writer can re-hash |
| R5 | Imported bundle restores attacker-supplied archives into the store where later audits, compares, and budgets trust them | B6 untrusted zip -> store | Low-Medium | Mitigated: zip-slip guard, member and uncompressed-size limits, chmod 0700 before extraction, partial-extraction cleanup, post-import audit. `bundle.py:286-344` |
| R6 | A `scenario run` hands the sibling load generator a full copy of the operator environment, including `SEVENDTD_TELNET_PASSWORD` and every other secret in it | B6 CLI -> sibling process | Low-Medium | Gap; no allowlist on the child env. `cli.py:967-990` |
| R7 | The bridge console verbs `apm reload`, `apm reset`, and `apm dump` are reachable by any account with console access; the bridge performs no authorization of its own | B4 console user -> bridge | Low | Inherent to the game's single-tier console auth. `BridgeMod.cs:294-345` |
| R8 | `/tmp/perf-<pid>.map` is a world-claimable name in a shared namespace | B5 CLI -> shared /tmp | Low | Mitigated: a foreign entry is refused before the atomic swap, and release is conditional on the link still pointing at this capture. `capture.py:502-557,619` |
| R9 | The `prometheus` command writes a metric file that an external scraper reads over a shared path, and every label in it comes from `summary.json`, which an imported bundle supplies | B6 store -> monitoring | Low | Mitigated: label values are escaped per the exposition spec and every number goes through a safe coercion, so a crafted summary degrades to a missing line instead of breaking the line format. `prometheus.py:22-26,29-148` |

Closed in the interval since the previous review: the `--telnet-password` argv
options (shell history and `/proc/<pid>/cmdline` exposure) are gone, so the
secret is env-only end to end; import now enforces member and byte limits and
chmods restored sessions 0700. The former R1, the perf-config ops switch, stays
removed: no `/api/perf` handler and no `Perf` class exist anywhere under
`bridge/`.

## Assets

| Asset | Where it lives | Impact if lost |
|---|---|---|
| Telnet password (`SEVENDTD_TELNET_PASSWORD`) | operator env, inherited by the app-scrape child and the loadgen child | Full console control of the game server (kick/ban/spawn/shutdown) |
| Raw telnet drain `app/bridge.jsonl` | session store, owner-only | Player names, IPs, Steam IDs disclosed |
| Server log excerpt `efficientserver_log_excerpt.txt` | session store, owner-only | Player identities; excluded from exports because it is PII by content, not by filename |
| Host/user identifiers in perf artifacts | perf.script, folded stacks, flame SVGs | Username and host paths leaked on sharing (home prefix scrubbed at export) |
| Session store evidence | `~/.local/share/7dtd-server-apm` (`SEVENDTD_APM_DIR`, `paths.py:55-57`) | Forged or destroyed measurement history |
| Exported metric line set | file written by `prometheus` (`prometheus.py:29-148`) | Layer, subsystem, and GC figures scraped into a shared monitoring stack; label values originate in an importable `summary.json` |
| Bridge telemetry dir | `Mods/7dtd-server-apm-bridge/telemetry/` (`BridgeMod.cs:34`) | JIT map files and snapshots readable by anything with install-dir access |
| `/tmp/perf-<pid>.map` | shared tmpfs (`capture.py:519-557`) | Symbol confusion for perf, or a local user's file unlinked by a root capture |

## Trust boundaries

| ID | Boundary | Crossing point(s) in code |
|---|---|---|
| B1 | Operator -> CLI | Typer options and env wiring in `tools/apm_suite/cli.py`; env overrides `SEVENDTD_APM_DIR`, `SEVENDTD_DS_DIR` (`paths.py:30-40,55-57`), `SEVENDTD_DS_BIN` and `SEVENDTD_DS_DIR` (`tools/host_profiler/find_server.sh:8-11`), `SEVENDTD_APM_PYTHON` (`tools/host_profiler/perf_record.sh:72`), `SEVENDTD_GAME_DIR` (`scripts/build_bridge.sh:30`) |
| B2 | CLI -> game server telnet (outbound network) | `socket.create_connection` in `capture.py:278`, `collectors/app_scrape.py:58`, `doctor.py:45` |
| B3 | Game server responses -> session store | Server-streamed console-log lines are cut at the first ISO timestamp before persistence (`app_scrape.py:34,44-56`); the `apm` command reply itself is persisted raw into `app/bridge.jsonl` |
| B4 | Dashboard web user / console user -> bridge (in-process, on stock hosts) | `GET /api/apm` on the stock V3 WebAPI scanner (`WebApi.cs:14-44`); `apm` console verbs (`BridgeMod.cs:294-345`); panel caller `bridge/ApmBridge/WebMod/bundle.ts:920-930` |
| B5 | CLI -> OS root, and CLI -> shared host namespaces | `sudo -n bpftrace` (`collectors.py:81-88`), `sudo -n mount --bind` / `umount` (`capture.py:189-193,213-238`), `sudo -n true` (`capture.py:493`); `/tmp/perf-<pid>.map` claim (`capture.py:519-557,619`); `scripts/check_bt.sh:49` |
| B6 | Other parties -> store and -> child processes | Sanitized export zip (`bundle.py:201-284`); untrusted import (`bundle.py:286-344`); metric file read by an external scraper (`prometheus.py:29-148`); loadgen child inherits a full environment copy (`cli.py:967-1010`) |

## Entry points

| Entry point | Kind | File |
|---|---|---|
| `capture`, `finalize`, `audit`, `verify-store`, `index`, `export`, `import`, `scaling`, `prometheus`, `monitor`, `prune`, `compare`, `budget`, `bridge`, `doctor`, `scenario run`, `scenario matrix`, `flame build`, `flame diff` | CLI arguments | `tools/apm_suite/cli.py:185-1267` |
| `SEVENDTD_TELNET_PASSWORD`, `SEVENDTD_APM_DIR`, `SEVENDTD_DS_DIR`, `SEVENDTD_DS_BIN`, `SEVENDTD_APM_PYTHON`, `SEVENDTD_GAME_DIR`, `APM_KEEP_SESSIONS`, `APM_PRUNE_GRACE_HOURS`, `LOADGEN_*` (emitted, not read) | env input | `cli.py:265,942,1194`, `paths.py:30-57`, `doctor.py:188`, `scripts/build_bridge.sh:30` |
| Telnet client actions (`apm dump/reset/jitmap/benchmark/reload`, rally, cleanup) | outbound network client | `capture.py:248-361`, `cli.py:942,1194` |
| Bridge `GET /api/apm` | HTTP GET, admin-gated, no request data read | `WebApi.cs:18-37` |
| Bridge console verbs `apm status/dump/reset/reload/capabilities/jitmap/benchmark` | console command | `BridgeMod.cs:294-345` |
| Zip bundle import | file parser (untrusted archive) | `bundle.py:286-344`, guard `io.py:105-120` |
| JSON/JSONL session parsing, incl. manifest-recorded artifact paths from an imported bundle | file parser (store-trusted, import-untrusted) | `io.py:168-219,105-120`; consumers in `analysis/` |
| `perf script` output, bpftrace maps, jit map | file parsers (host-produced) | `tools/host_profiler/stackcollapse_perf.py`, `analysis/report.py`, `analysis/jitsym.py`, `analysis/events.py`; fuzzed in `tools/apm_suite/tests/test_fuzz_parsers.py` |
| Bridge config `Config/apmbridge.json` | file parser (operator-authored) | `BridgeConfig.cs:20-39` |
| Collector subprocesses (bpftrace, perf via `hw_perf.sh` / `perf_record.sh`, `preprocess_bt.py`, `app_scrape.py`, `make_flames.sh`) | child processes from CLI-built argv | `collectors.py:62-195`, `capture.py:830-839` |
| Sibling loadgen launcher | child process script | `cli.py:1005-1010` |
| Prometheus exposition file, read by an external scraper | artifact rendering (labels from an importable `summary.json`) | `prometheus.py:29-148`, CLI command `cli.py:539-559` |
| Generated HTML/SVG reports opened in a browser | artifact rendering | `tools/host_profiler/interactive_flame.py:328-342` |

## Threats per boundary (STRIDE, concrete)

**B1 operator -> CLI**
- Tampering: env overrides (`SEVENDTD_APM_DIR`, `SEVENDTD_DS_DIR`) redirect all
  reads and writes to a chosen tree (`paths.py:30-56`). A hostile tree supplies
  the bpftrace scripts and shell helpers the collectors execute.
- Information disclosure: none via argv. There is no `--telnet-password` flag;
  the secret is read from the environment at each call site.
- Repudiation: none. The tool is single-operator and records no invocation log.

**B2 CLI -> telnet**
- Spoofing and information disclosure: the client authenticates the server by
  TCP reachability alone. A rogue listener receives the password in cleartext
  on the first exchange (`app_scrape.py:58`, `capture.py:279`). Telnet is
  plaintext end to end; this is the game interface, not a choice made here.
- DoS: bounded by socket timeouts (0.5s doctor probe, 3s capture, 5s scrape).
- Note: `doctor.py:47-54` states explicitly that reachability is not
  authentication, so a passing doctor run must not be read as a server identity
  check.

**B3 server responses -> store**
- Tampering and injection: persisted replies are server-controlled text written
  verbatim into `bridge.jsonl`. The streamed log prefix carrying player data is
  stripped first (`app_scrape.py:44-56`); owner-only perms and export exclusion
  are the outer layers.
- DoS: the scrape window bounds record size (`app_scrape.py:100-110`); no cap
  on a single reply beyond the socket timeout.

**B4 dashboard / console user -> bridge**
- Elevation: the REST endpoint relies wholly on stock dashboard auth;
  `DefaultMethodPermissionLevels()` returns admin-only zeros for every verb and
  the handler reads no request data (`WebApi.cs:18-37`). No mutating verb is
  overridden, so the base handler answers 405. `tests/test_bridge_build_surface.py:71-80`
  pins the all-zero array.
- The console path is the exception: the bridge authorizes nothing itself, takes
  `CommandSenderInfo` and never inspects it (`BridgeMod.cs:285`). `apm reload`
  and `apm reset` re-read config and clear counters for any console-level
  account, and `apm dump` writes a file (`BridgeMod.cs:300-307`).
- Read-only surface on the web side: the single endpoint answers a snapshot or a
  coded `SNAPSHOT_FAILED` 500 whose exception text stays in the log
  (`WebApi.cs:24-33`). The bridge opens no listener of its own and starts no
  watcher, thread pool fan-out beyond the single-flight export worker, or child
  process.

**B5 CLI -> root and shared namespaces**
- Elevation of privilege: collector argv embeds operator-supplied `--pid` and
  resolved paths (`collectors.py:80-88`, `capture.py:189-193`). No call site
  uses a shell, so injection through the pid or path is not available, but the
  elevation is unconditional once `sudo -n` succeeds (`capture.py:692`). R2.
- The one place external data reaches a root-privileged bpftrace program is
  `preprocess_bt.py`, which rejects `--comm` containing quote, backslash, or
  newline and restricts `--mono-so` to `[\w./+\-]+` before writing the script
  (`preprocess_bt.py:47-54,66-73`).
- Shared-namespace tampering: `/tmp/perf-<pid>.map` is claimable by any local
  user. The swap refuses any non-symlink or foreign-uid entry, and removal only
  fires when the link still points at this capture's target
  (`capture.py:502-557`). R8.

**B6 other parties -> store and -> child processes**
- Tampering: forged evidence feeds `audit`/`compare`/`budget` verdicts. Manifest
  hashes are checked at finalize and import (`session.py:475-537`), and a
  bundle can no longer plant a path that escapes its session directory
  (`io.py:105-120`), but the store carries no signatures, so a local writer can
  re-hash. R4.
- Import hardening, all in `bundle.py:286-344`: member-count and declared-byte
  ceilings (20k members, 2 GiB, `bundle.py:40-41`), per-member path validation
  (`bundle.py:305`), exclusive-create directory claim, chmod 0700 before
  extraction (`bundle.py:322-323`), removal of a partial session on
  `BadZipFile`/`zlib.error`/`OSError`, and a post-import audit. Attacker-controlled
  member names are HTML-escaped before printing so a crafted string cannot
  rewrite console styling.
- Information disclosure on export: the bundle walk skips symlinks and any
  member whose name matches the PII set or the `efficientserver` / `output_log`
  content markers (`bundle.py:48-57,230-240`), and redacts `cmdline`, `exe`,
  and the home prefix (`bundle.py:75-96`). Restored sessions are owner-only,
  matching captured ones, so `docs/APM.md:130-132` is accurate for both paths.
- Metric-file injection: `prometheus` reads layer, subsystem, and cause names
  out of `summary.json` and the lag frame, so an imported bundle chooses them.
  `_prom_label` escapes `\`, `"`, and newline, and every numeric field goes
  through `as_number`, so a crafted summary drops a line instead of breaking
  the exposition format (`prometheus.py:22-26,48-55,84-90`).
- Child-process exposure: the loadgen subprocess receives `os.environ.copy()`
  plus its 17 `LOADGEN_*` keys (`cli.py:967-990`), so it holds the telnet
  password and anything else in the operator environment. R6.

## Abuse cases

- **Arbitrary-process profiling (local):** an operator, or anything running as
  them, passes `--pid <victim>`; capture resolves `/proc/<pid>/exe`, bind-mounts
  its Mono library, and runs root profilers against it
  (`capture.py:189-193,692,919-921`). The tool performs a privilege transition
  on behalf of whoever can invoke it.
- **Hostile toolchain tree:** pointing `SEVENDTD_DS_DIR` or `SEVENDTD_APM_DIR`
  at a tree the attacker wrote supplies the `.bt` sources, `preprocess_bt.py`,
  and shell helpers that later run, partly as root.
- **PII harvesting via shared artifacts:** raw sessions hold the full telnet
  drain and the operator's log excerpt (`docs/APM.md:111-123`); anyone who can
  read the store, or a backup taken without the 0700 mode, gets it. Exported
  bundles drop both classes (`bundle.py:48-57,201-284`).
- **Bundle-borne manifest paths:** an imported bundle supplies its own
  `manifest.json`, so its recorded artifact paths are attacker input joined onto
  a session directory. `member_is_safe` is the named validation point.
- **Monitored-host fingerprinting through the metric file:** a `prometheus`
  run over an imported bundle republishes that bundle's layer names, managed
  subsystem names, GC pause worst case, and UDP send rate into whatever
  monitoring stack scrapes the path. Those names and values are attacker-chosen
  strings that survive into a system an operator trusts (`prometheus.py:48-141`).

## Mitigations that exist (with evidence)

| Control | Covers | File |
|---|---|---|
| No `--telnet-password` flag anywhere; secret read from env at each call site | R1 | `cli.py:265,942,1194`, `app_scrape.py:106` |
| Password passed to the scrape child via env, never child argv | R1, R6 (child side) | `collectors.py:128` |
| Doctor reports the secret as a set/unset boolean, never its value | secret leakage into reports | `doctor.py:188` |
| Loud warning when the app layer needs an unset password | misconfiguration | `capture.py:471-483` |
| Captured sessions chmod 0700 before any artifact lands | R3 | `capture.py:684` |
| Import: member and byte ceilings, path validation, chmod 0700 pre-extract, partial-extraction cleanup, post-import audit | R5 | `bundle.py:40-41,286-344` |
| Shared path guard for zip members and manifest-recorded artifact paths | R5, B6 tampering | `io.py:105-120`, used at `bundle.py:305` and `session.py:17` |
| Lone-surrogate scrubbing in every JSON/JSONL reader | parser crash on hostile store content | `io.py:18-34`, `io.py:168-219` |
| UTF-8 pinned on stdout, stderr, and stdin so a bare systemd or `sudo` environment cannot turn a reported path into a traceback | local DoS of the reporting path | `io.py:37-66` |
| `/tmp/perf-<pid>.map` refuses foreign entries; conditional release; atomic swap | R8 | `capture.py:502-557` |
| Export excludes the raw drain, server log excerpt (by name or by `efficientserver` / `output_log` marker), perf data, stderr, manifest; redacts cmdline, exe, home prefix; skips symlinks; writes through a temp archive and `os.replace` | R3 | `bundle.py:48-57,75-96,201-284` |
| Prometheus label escaping and numeric coercion, so a crafted `summary.json` drops a metric instead of breaking the exposition format | R9 | `prometheus.py:22-26,29-148` |
| Interactive flame page escapes the embedded tree JSON (`<`, `>`, `&`) and html-escapes title and file name, so a hostile frame name cannot break out of `<script>` | stored XSS in a shared report | `interactive_flame.py:328-339` |
| No `shell=True`, `os.system`, or command-string concatenation anywhere in `tools/`; every subprocess is an argv list | command injection | `collectors.py`, `capture.py`, `cli.py` |
| bpftrace preprocessor types `--pid` as `int` and validates `--comm` and `--mono-so` before generating a root-run script | root-program injection | `preprocess_bt.py:48-63,74-81` |
| Bridge endpoint admin-only on every verb, GET-only, reads no request data | B4 web scope | `WebApi.cs:18-37` |
| Bridge console verbs restricted by an allowlist with `int.TryParse` on the numeric arg | B4 console scope | `BridgeMod.cs:283-285,290-330` |
| Bridge config rejects unknown keys and clamps values on load; malformed config falls back to defaults | hostile config | `BridgeConfig.cs:50,98-110` |
| Bridge writes are atomic (temp plus replace) with temp cleanup on failure | partial telemetry | `TempFiles.cs:34-46`, `Telemetry.cs:453-457` |
| Every instrumentation hook swallows its own exceptions | bridge-caused server crash | `BridgeMod.cs:218-239` |
| Crash-safe atomic writes plus parent directory fsync; the `mkstemp` temp inherits 0600 and `replace` carries that mode over, so the metric file and the export archive land owner-only | local disclosure of the metric file or bundle | `io.py:138-161`, used at `prometheus.py:148` and `bundle.py:213,282` |
| Monitor sample log rotation at 64 MiB, one generation kept | local disk exhaustion from a 24/7 run | `cli.py:103-130` |
| Prune trash grace window (`APM_PRUNE_GRACE_HOURS`) | accidental destruction | `session.py`, `docs/APM.md` |
| Server-streamed telnet log lines dropped before persistence | PII in the store | `app_scrape.py:34,44-56` |
| `lint-webui.sh` pins the fetched anti-slop tarball by SHA-256, and caches the extracted source under that commit so a pin bump re-verifies | supply chain for the lint tool | `scripts/lint-webui.sh:21,35-39,65-67` |

## Gaps (ranked; fixes belong to sec-review)

- **G1:** No shipped sudoers policy constrains the `sudo -n` surface the
  collectors require. The `Makefile` and `scripts/check_bt.sh:20-23` document
  the dependency but nothing pins it, so passwordless sudo plus this repo
  means profiling any pid (R2).
- **G2:** The loadgen child inherits the entire operator environment rather
  than an allowlist plus the 17 `LOADGEN_*` keys it needs (`cli.py:967-990`),
  handing it the telnet password and any other secret (R6).
- **G3:** No SECURITY.md, so no disclosure contact, supported-version
  statement, or vulnerability-handling path exists anywhere in the repo.
- **G4:** Nothing in the store is signed, so a local writer with write access
  can re-hash forged artifacts into something audit reports as valid (R4).
- **G5:** Telnet authentication trusts whatever answers the port; no host
  identity pinning is possible over the plaintext game interface. Recorded so
  nobody claims otherwise. A caller that needs server identity must verify it
  out of band before pointing the CLI at a remote port.
- **G6:** The bridge console verbs carry no sender authorization of their own
  (R7). Inherent to the game's console tiers.

## Response readiness (note only)

- Forensic trail: each capture writes versioned metadata, collector results,
  and hash manifests under the session dir (`meta.json`, `finalize.py`),
  giving an investigator per-artifact integrity checks; `verify-store`
  (`cli.py:344-402`) runs a read-only audit across a whole store for restore
  drills. The bridge logs to the game log via `Log.Out` (`BridgeMod.cs:278`).
  o11y-review owns log structure. No central audit of CLI invocations exists:
  who ran what, and under which environment, is not recorded anywhere. An
  imported session is indistinguishable from a captured one in the store once
  it passes the post-import audit, so the store records no provenance for it.
- Vulnerability-to-fix path: undocumented (G3).

## Related

- Capture lifecycle and validity: `docs/APM.md`
- Bridge schema and overhead controls: `bridge/README.md`
