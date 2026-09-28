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
- **Last reviewed:** 2026-09-28, against commit `b5afd97`. Owner and review
  cadence: not assigned.
- **Disclosure path:** none documented. There is no SECURITY.md; until one
  exists there is no stated route from "vulnerability reported" to "fix
  shipped" (see Response readiness).

## Risk-ranked summary

| # | Risk | Boundary | Severity | Status |
|---|---|---|---|---|
| R1 | The telnet password is sent in cleartext to whatever answers `--telnet-port`, with no host identity check, so a rogue or redirected listener harvests full console control of the game server | B2 CLI -> telnet | Medium-High | Gap. Inherent to the stock plaintext telnet interface; not fixable in this repo. `capture.py:266-300`, `app_scrape.py:58` |
| R2 | Root-adjacent collectors driven by operator input: every capture shells out to `sudo -n bpftrace`, `perf`, and `mount --bind` against an operator-chosen `--pid`, so anyone who can invoke the CLI with passwordless sudo can profile arbitrary processes | B5 CLI -> root | Medium | Gap; no sudoers policy ships with the repo to constrain it |
| R3 | Session store leaks player PII (names, IPs, Steam IDs) if raw sessions leave the host | B3/B6 store -> other parties | Medium | Mitigated: owner-only perms on both captured and imported sessions, raw drain excluded from exports |
| R4 | Evidence integrity: a writable store lets a local attacker forge measurements that feed baseline/candidate verdicts | B6 store -> analysis | Low-Medium | Partially mitigated. Manifest-recorded artifact paths are validated at read, and hashes are checked at finalize/import, but nothing is signed, so a local writer can re-hash |
| R5 | Imported bundle restores attacker-supplied archives into the store where later audits, compares, and budgets trust them | B6 untrusted zip -> store | Low-Medium | Mitigated since the previous pass: zip-slip guard, member and uncompressed-size limits, chmod 0700 before extraction, partial-extraction cleanup, post-import audit |
| R6 | A `scenario run` hands the sibling load generator a full copy of the operator environment, including `SEVENDTD_TELNET_PASSWORD` and every other secret in it | B6 CLI -> sibling process | Low-Medium | Gap; no allowlist on the child env |
| R7 | The bridge console verbs `apm reload`, `apm reset`, and `apm dump` are reachable by any account with console access; the bridge performs no authorization of its own | B4 console user -> bridge | Low | Inherent to the game's single-tier console auth. `BridgeMod.cs:276-324` |
| R8 | `/tmp/perf-<pid>.map` is a world-claimable name in a shared namespace | B5 CLI -> shared /tmp | Low | Mitigated: a foreign entry is refused before the atomic swap, and release is conditional on the link still pointing at this capture. `capture.py:502-557` |

Closed in the interval since the previous review: the `--telnet-password` argv
options (shell history and `/proc/<pid>/cmdline` exposure) are gone, so the
secret is env-only end to end; import now enforces member and byte limits and
chmods restored sessions 0700; `docs/APM.md:123` now states the import case.
The former R1, the perf-config ops switch, stays removed: no `/api/perf`
handler and no `Perf` class exist anywhere under `bridge/`.

## Assets

| Asset | Where it lives | Impact if lost |
|---|---|---|
| Telnet password (`SEVENDTD_TELNET_PASSWORD`) | operator env, inherited by the app-scrape child and the loadgen child | Full console control of the game server (kick/ban/spawn/shutdown) |
| Raw telnet drain `app/bridge.jsonl` | session store, owner-only | Player names, IPs, Steam IDs disclosed |
| Server log excerpt `efficientserver_log_excerpt.txt` | session store, owner-only | Player identities; excluded from exports because it is PII by content, not by filename |
| Host/user identifiers in perf artifacts | perf.script, folded stacks, flame SVGs | Username and host paths leaked on sharing (home prefix scrubbed at export) |
| Session store evidence | `~/.local/share/7dtd-server-apm` (`SEVENDTD_APM_DIR`, `paths.py:54-56`) | Forged or destroyed measurement history |
| Bridge telemetry dir | `Mods/7dtd-server-apm-bridge/telemetry/` (`BridgeMod.cs:34`) | JIT map files and snapshots readable by anything with install-dir access |
| `/tmp/perf-<pid>.map` | shared tmpfs (`capture.py:519-557`) | Symbol confusion for perf, or a local user's file unlinked by a root capture |

## Trust boundaries

| ID | Boundary | Crossing point(s) in code |
|---|---|---|
| B1 | Operator -> CLI | Typer options and env wiring in `tools/apm_suite/cli.py`; env overrides `SEVENDTD_APM_DIR`, `SEVENDTD_DS_DIR` (`paths.py:30-40,54-56`), `SEVENDTD_DS_BIN` and `SEVENDTD_DS_DIR` (`tools/host_profiler/find_server.sh:8-11`), `SEVENDTD_APM_PYTHON` (`tools/host_profiler/perf_record.sh:72`) |
| B2 | CLI -> game server telnet (outbound network) | `socket.create_connection` in `capture.py:279`, `collectors/app_scrape.py:58`, `doctor.py:46` |
| B3 | Game server responses -> session store | Server-streamed console-log lines are cut at the first ISO timestamp before persistence (`app_scrape.py:34,44-56`); the `apm` command reply itself is persisted raw into `app/bridge.jsonl` |
| B4 | Dashboard web user / console user -> bridge (in-process, on stock hosts) | `GET /api/apm` on the stock V3 WebAPI scanner (`WebApi.cs:14-41`); `apm` console verbs (`BridgeMod.cs:276-324`); panel caller `bridge/ApmBridge/WebMod/bundle.ts:902-906` |
| B5 | CLI -> OS root, and CLI -> shared host namespaces | `sudo -n bpftrace` (`collectors.py:80-88`), `sudo -n mount --bind` / `umount` (`capture.py:189-193,213-238`), `sudo -n true` (`capture.py:493-496`, `doctor.py:28`); `/tmp/perf-<pid>.map` claim (`capture.py:519-557`); `scripts/check_bt.sh:49` |
| B6 | Other parties -> store and -> child processes | Sanitized export zip (`cli.py:505-606`); untrusted import (`cli.py:619-700`); loadgen child inherits a full environment copy (`cli.py:1288-1331`) |

## Entry points

| Entry point | Kind | File |
|---|---|---|
| `capture`, `finalize`, `audit`, `verify-store`, `index`, `export`, `import`, `scaling`, `prometheus`, `monitor`, `prune`, `compare`, `budget`, `bridge`, `doctor`, `scenario run`, `scenario matrix`, `flame build`, `flame diff` | CLI arguments | `tools/apm_suite/cli.py` |
| `SEVENDTD_TELNET_PASSWORD`, `SEVENDTD_APM_DIR`, `SEVENDTD_DS_DIR`, `SEVENDTD_DS_BIN`, `SEVENDTD_APM_PYTHON`, `APM_KEEP_SESSIONS`, `APM_PRUNE_GRACE_HOURS`, `LOADGEN_*` (emitted, not read) | env input | `cli.py:224,1264,1511`, `paths.py:30-56`, `doctor.py:184-190` |
| Telnet client actions (`apm dump/reset/jitmap/benchmark/reload`, rally, cleanup) | outbound network client | `capture.py:249-361`, `cli.py:1353,1545` |
| Bridge `GET /api/apm` | HTTP GET, admin-gated, no request data read | `WebApi.cs:18-41` |
| Bridge console verbs `apm status/dump/reset/reload/capabilities/jitmap/benchmark` | console command | `BridgeMod.cs:276-324` |
| Zip bundle import | file parser (untrusted archive) | `cli.py:619-700`, guard `io.py:36-50` |
| JSON/JSONL session parsing, incl. manifest-recorded artifact paths from an imported bundle | file parser (store-trusted, import-untrusted) | `io.py:17-33,36-50,99-134`; consumers in `analysis/` |
| `perf script` output, bpftrace maps, jit map | file parsers (host-produced) | `tools/host_profiler/stackcollapse_perf.py`, `analysis/report.py`, `analysis/jitsym.py`, `analysis/events.py`; fuzzed in `tools/apm_suite/tests/test_fuzz_parsers.py` |
| Bridge config `Config/apmbridge.json` | file parser (operator-authored) | `BridgeConfig.cs:20-39` |
| Collector subprocesses (bpftrace, perf via `hw_perf.sh` / `perf_record.sh`, `preprocess_bt.py`, `app_scrape.py`, `make_flames.sh`) | child processes from CLI-built argv | `collectors.py:62-195`, `capture.py:830-839` |
| Sibling loadgen launcher | child process script | `cli.py:1277,1331` |
| Generated HTML/SVG reports opened in a browser | artifact rendering | `tools/host_profiler/interactive_flame.py:155` |

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
  the handler reads no request data (`WebApi.cs:18-41`). No mutating verb is
  overridden, so the base handler answers 405. `tools/apm_suite/tests/test_bridge_build_surface.py:71-80`
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
  hashes are checked at finalize and import (`io.py:181-186`, `session.py:517+`),
  and a bundle can no longer plant a path that escapes its session directory
  (`io.py:36-50`), but the store carries no signatures, so a local writer can
  re-hash. R4.
- Import hardening, all in `cli.py:619-700`: member-count and declared-byte
  ceilings (20k members, 2 GiB, `cli.py:615-616,641-648`), per-member path
  validation, exclusive-create directory claim, chmod 0700 before extraction,
  removal of a partial session on `BadZipFile`/`zlib.error`/`OSError`, and a
  post-import audit. Attacker-controlled member names are HTML-escaped before
  printing so a crafted string cannot rewrite console styling.
- Information disclosure: restored sessions are owner-only, matching captured
  ones, so `docs/APM.md:123` is accurate for both paths.
- Child-process exposure: the loadgen subprocess receives `os.environ.copy()`
  plus its 17 `LOADGEN_*` keys (`cli.py:1288-1312`), so it holds the telnet
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
  bundles drop both classes (`cli.py:520-529,505-606`).
- **Bundle-borne manifest paths:** an imported bundle supplies its own
  `manifest.json`, so its recorded artifact paths are attacker input joined onto
  a session directory. `member_is_safe` is the named validation point.

## Mitigations that exist (with evidence)

| Control | Covers | File |
|---|---|---|
| No `--telnet-password` flag anywhere; secret read from env at each call site | R1 | `cli.py:224,1264,1511`, `app_scrape.py:106` |
| Password passed to the scrape child via env, never child argv | R1, R6 (child side) | `collectors.py:128`, `capture.py:898-900` |
| Doctor reports the secret as a set/unset boolean, never its value | secret leakage into reports | `doctor.py:184-190` |
| Loud warning when the app layer needs an unset password | misconfiguration | `capture.py:472-483`, called at `capture.py:689-690` |
| Captured sessions chmod 0700 before any artifact lands | R3 | `capture.py:684-686` |
| Import: member and byte ceilings, path validation, chmod 0700 pre-extract, partial-extraction cleanup, post-import audit | R5 | `cli.py:615-616,641-700` |
| Shared path guard for zip members and manifest-recorded artifact paths | R5, B6 tampering | `io.py:36-50` |
| Lone-surrogate scrubbing in every JSON/JSONL reader | parser crash on hostile store content | `io.py:17-33` |
| `/tmp/perf-<pid>.map` refuses foreign entries; conditional release; atomic swap | R8 | `capture.py:502-557` |
| Export excludes the raw drain, server log excerpt, perf data, stderr, manifest; redacts cmdline, exe, home prefix; skips symlinks | R3 | `cli.py:520-529,557-563,505-606` |
| No `shell=True`, `os.system`, or command-string concatenation anywhere in `tools/`; every subprocess is an argv list | command injection | `collectors.py`, `capture.py`, `cli.py` |
| bpftrace preprocessor validates `--comm` and `--mono-so` before generating a root-run script | root-program injection | `preprocess_bt.py:47-54,66-73` |
| Bridge endpoint admin-only on every verb, GET-only, reads no request data | B4 web scope | `WebApi.cs:18-41` |
| Bridge console verbs restricted by an allowlist with `int.TryParse` on the numeric arg | B4 console scope | `BridgeMod.cs:280-281,287-321` |
| Bridge config values clamped on load; malformed config falls back to defaults | hostile config | `BridgeConfig.cs:26-39` |
| Bridge writes are atomic (temp plus replace) with temp cleanup on failure | partial telemetry | `TempFiles.cs:34-46`, `Telemetry.cs:453-457` |
| Every instrumentation hook swallows its own exceptions | bridge-caused server crash | `BridgeMod.cs:218-239` |
| Crash-safe atomic writes plus parent directory fsync | evidence durability | `io.py:53-96` |
| Monitor sample log rotation at 64 MiB, one generation kept | local disk exhaustion from a 24/7 run | `cli.py:89-110` |
| Prune trash grace window (`APM_PRUNE_GRACE_HOURS`) | accidental destruction | `session.py`, `docs/APM.md` |
| Server-streamed telnet log lines dropped before persistence | PII in the store | `app_scrape.py:34,44-56` |
| `lint-webui.sh` pins the fetched anti-slop tarball by SHA-256, and caches the extracted source under that commit so a pin bump re-verifies | supply chain for the lint tool | `scripts/lint-webui.sh:37-40,62-84` |

## Gaps (ranked; fixes belong to sec-review)

- **G1:** No shipped sudoers policy constrains the `sudo -n` surface the
  collectors require. The `Makefile` and `scripts/check_bt.sh:20-23` document
  the dependency but nothing pins it, so passwordless sudo plus this repo
  means profiling any pid (R2).
- **G2:** The loadgen child inherits the entire operator environment rather
  than an allowlist plus the 17 `LOADGEN_*` keys it needs (`cli.py:1288-1312`),
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
  (`cli.py:305-362`) runs a read-only audit across a whole store for restore
  drills. The bridge logs to the game log via `Log.Out` (`BridgeMod.cs:253`).
  o11y-review owns log structure. No central audit of CLI invocations exists:
  who ran what, and under which environment, is not recorded anywhere.
- Vulnerability-to-fix path: undocumented (G3).

## Related

- Capture lifecycle and validity: `docs/APM.md`
- Bridge schema and overhead controls: `bridge/README.md`
