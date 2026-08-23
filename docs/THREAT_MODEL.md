# Threat model

Systemic view of what this repository's tooling can be attacked through, what
it costs, and which controls exist. Point vulnerabilities and fixes belong to
sec-review; this document is the map that aims those passes.

- **Scope:** host-only measurement CLI (`7dtd-apm`, `tools/apm_suite/`), shell
  and bpftrace collectors (`tools/apm/`, `tools/host_profiler/`), and the
  optional in-server bridge DLL (`bridge/ApmBridge/`). The game server itself,
  its stock WebDashboard implementation, and sibling projects
  (`7dtd-loadgen`, `7dtd-optimizer`) are outside this model; only the
  interfaces between them and this repo are modeled.
- **Last reviewed:** 2026-08-23 (from commit e29d4d4). Owner and review
  cadence: not yet assigned.
- **Disclosure path:** none documented. There is no SECURITY.md; until one
  exists there is no stated route from "vulnerability reported" to "fix
  shipped" (see Response readiness).

## Risk-ranked summary

| # | Risk | Boundary | Severity | Status |
|---|---|---|---|---|
| R1 | Admin-web compromise yields remote server shutdown: `POST /api/perf` executes a console `shutdown` after flipping config | B4 web user -> bridge | High | Accepted by design (ops switch); single control = dashboard auth |
| R2 | Telnet password exposed via argv: `capture --telnet-password` / `scenario --telnet-password` options put the secret in the invoking shell history and the process's own `/proc/<pid>/cmdline`, contradicting the env-only guidance | B1 operator -> CLI | Medium-High | Gap (`tools/apm_suite/cli.py:120-124`, `cli.py:806-808`) |
| R3 | Root-adjacent collectors driven by operator input: every capture shells out to `sudo -n bpftrace/perf/mount` with an operator-chosen `--pid`; anyone able to run the CLI against passwordless sudo can profile arbitrary processes | B5 CLI -> root | Medium | Gap; no sudoers policy shipped to constrain it |
| R4 | Session store leaks player PII (names, IPs, Steam IDs) if raw sessions leave the host; protection is filesystem permissions only | B3/B6 store -> other parties | Medium | Mitigated for captured sessions (chmod 0700), not for imported ones (G1) |
| R5 | Imported bundle tampering: `import` restores attacker-supplied archives into the store where later audits/compares trust them | B6 untrusted zip -> store | Medium | Partially mitigated (zip-slip guard, audit); no size limits |
| R6 | Evidence integrity: a writable store lets a local attacker forge measurements that feed baseline/candidate verdicts | B6 store -> analysis | Low-Medium | Partially mitigated (integrity manifests, hash audit at finalize/import) |

## Assets

| Asset | Where it lives | Impact if lost |
|---|---|---|
| Telnet password (`SEVENDTD_TELNET_PASSWORD`) | env of operator + child scrapers | Full console control of the game server (kick/ban/spawn/shutdown) |
| Raw telnet drain `app/bridge.jsonl` | session store, owner-only | Player names, IPs, Steam IDs disclosed |
| Host/user identifiers in perf artifacts | perf.script, folded stacks, flame SVGs | Username/host paths leaked on sharing (home prefix scrubbed at export) |
| Session store evidence | `~/.local/share/7dtd-apm` (`SEVENDTD_APM_DIR`), `tools/apm_suite/paths.py:25` | Forged or destroyed measurement history |
| Game server availability | restarted by `POST /api/perf` (`bridge/ApmBridge/WebApi.cs:270-278`) | Downtime per flip |
| Bridge telemetry dir | `Mods/7dtd-apm-bridge/telemetry/` inside the server install (`bridge/ApmBridge/BridgeMod.cs:29`) | JIT map files readable by anything with install-dir access |

## Trust boundaries

| ID | Boundary | Crossing point(s) in code |
|---|---|---|
| B1 | Operator -> CLI | Typer options/env wiring, `tools/apm_suite/cli.py`; env overrides `SEVENDTD_APM_DIR`, `SEVENDTD_DS_DIR` (`paths.py:12-27`), `APM_PRUNE_GRACE_HOURS`, `APM_KEEP_SESSIONS` (`docs/APM.md`) |
| B2 | CLI -> game server telnet (outbound network) | `socket.create_connection` in `capture.py:434-497`, `collectors/app_scrape.py:20`, `doctor.py:37-47` |
| B3 | Game server responses -> session store | Server-controlled banner/log lines discarded pre-persistence (`app_scrape.py:41-49`); requested `apm` replies persisted raw into `app/bridge.jsonl` |
| B4 | Dashboard web user -> bridge REST (inbound listener inside server process) | `GET /api/apm`, `GET/POST /api/perf` registered on the stock V3 WebAPI scanner (`WebApi.cs:14,52`); UI caller `bridge/ApmBridge/WebMod/bundle.ts:757,763` |
| B5 | CLI -> OS root | `sudo -n` for bpftrace/perf (`capture.py:97-105`), `sudo -n mount --bind` (`capture.py:380-391`) |
| B6 | Other parties -> store | Sanitized export zip (`cli.py:249-313`); untrusted import (`cli.py:322-357`); sibling loadgen subprocess inherits full environment (`cli.py:827-869`) |

## Entry points

| Entry point | Kind | File |
|---|---|---|
| `7dtd-apm capture/scenario run/scenario matrix/monitor/audit/compare/budget/export/import/prune/doctor` | CLI arguments | `tools/apm_suite/cli.py` |
| `--telnet-password`, `--pid`, `SEVENDTD_*`, `LOADGEN_*`, `APM_*` env vars | argv/env input | `cli.py:120,806,938`, `paths.py`, `doctor.py:164` |
| Telnet client actions (`apm dump/reset/jitmap`, `listplayers`, teleport/rally) | outbound network client | `capture.py:434-530`, `cli.py:882,987` |
| Bridge `GET /api/apm` | HTTP GET, admin-gated | `WebApi.cs:18-41` |
| Bridge `GET/POST /api/perf` | HTTP GET/POST, admin-gated; POST restarts server | `WebApi.cs:156-282` |
| Bridge console commands `apm <dump/reset/reload/capabilities/jitmap/benchmark>` | telnet/console command | `bridge/ApmBridge/BridgeMod.cs:256-275` |
| Zip bundle import | file parser (untrusted archive) | `cli.py:322-357` |
| JSON/JSONL session parsing | file parser (store-trusted) | `io.py:46-50`, `analysis/*` |
| Collector subprocesses (bpftrace, perf, preprocess_bt.py, threads.py) | child processes from CLI-built argv | `capture.py:76-160` |
| Sibling loadgen launcher | child process script | `cli.py:816,869` |
| Generated HTML/SVG reports opened in a browser | artifact rendering | `host_profiler/interactive_flame.py` (labels XML-escaped, line 155) |

## Threats per boundary (STRIDE, concrete)

**B1 operator -> CLI**
- Spoofing/tampering: none beyond local account compromise; env overrides
  (`SEVENDTD_APM_DIR`) redirect all reads/writes to an attacker-chosen tree
  (`paths.py:12-27`). Information disclosure: R2 argv secret.
- Repudiation: none (single-user tool; no multi-operator identity).

**B2 CLI -> telnet**
- Spoofing: the client authenticates the server by nothing but TCP reach;
  a rogue listener receives the password in cleartext first thing
  (`app_scrape.py:46-49`). Telnet is plaintext end-to-end (game limitation).
- DoS: bounded sockets/timeouts throughout (`timeout=3..5`).

**B3 server responses -> store**
- Tampering/injection: persisted replies are server-controlled text written
  verbatim into `bridge.jsonl`; mitigated by owner-only perms and exclusion
  from exports. Banner/log noise (player PII) is dropped before persistence
  (`app_scrape.py:41-49`).
- DoS: scrape window bounds record size; no cap on reply length beyond
  socket timeouts.

**B4 web user -> bridge REST**
- Elevation: endpoints rely wholly on the stock dashboard auth;
  `DefaultMethodPermissionLevels()` returns admin-only zeros for every verb
  (`WebApi.cs:37-41,281`); `Apm` implements GET only, while `Perf` implements
  GET and POST (`WebApi.cs:18,156,173`). Any bypass of dashboard auth is out
  of repo scope but lands here.
- DoS/privilege abuse: `POST /api/perf` flips config then invokes
  `SdtdConsole.Instance.ExecuteSync("shutdown", ...)` (`WebApi.cs:270-278`);
  an authenticated admin can loop restarts (R1). Writes are allowlisted to
  known group keys (`SetGroup`, `WebApi.cs:133-154`) so no arbitrary config
  injection.
- Tampering: config read-modify-write serialized under a lock
  (`WebApi.cs:92,196-238`).

**B5 CLI -> root**
- Elevation of privilege: collector argv embeds operator-supplied `--pid`
  and resolved paths (`capture.py:76-107`); argument lists (no shell) prevent
  injection, but the elevation itself is unconditional once sudo -n succeeds
  (`capture.py:664-667`). R3.

**B6 others -> store**
- Tampering: forged evidence feeds `audit/compare/budget` verdicts; partial
  control via finalize integrity manifests and SHA-256 audit
  (`io.py:97-101`, `session.py` audit path). Store files carry no signatures,
  so a local writer can re-hash (R6).
- DoS: `import` has a zip-slip guard (`cli.py:316-341`) and Python's
  `extractall` writes symlink members as regular files, but nothing bounds
  decompressed size or member count (R5, local disk exhaustion).
- Information disclosure: imported sessions keep default umask permissions,
  while `docs/APM.md:106` claims sessions are chmod 0700; captured sessions
  do get 0700 (`capture.py:654-658`). Drift recorded as G1 below.

## Abuse cases

- **Restart gaming (authenticated):** a hostile dashboard admin repeatedly
  POSTs `{"enabled": ...}` to `/api/perf`; each real change schedules a
  server shutdown (`WebApi.cs:267-278`). Enabling code path named above;
  impact is availability only, config writes stay allowlisted.
- **Arbitrary-process profiling (local):** an operator (or anything running
  as them) passes `--pid <victim>`; capture resolves `/proc/<pid>/exe`,
  bind-mounts its Mono library, and runs root profilers against it
  (`capture.py:380-391,664-667`). The tool performs privilege transitions on
  behalf of whoever can invoke it.
- **PII harvesting via shared artifacts:** raw sessions hold the full telnet
  drain including player identities (`docs/APM.md:102-109`); anyone who can
  read the store (same account, or backups made without the 0700 mode) gets
  it. Exported bundles strip this class of data (`cli.py:254` exclusions).

## Mitigations that exist (with evidence)

| Control | Covers | File |
|---|---|---|
| Password delivered to children via env only, never child argv | R2 (child side) | `capture.py:143` |
| Display redaction of password flags when spawning sibling tools | log leakage | `runner.py:22-28` |
| Loud warning when the app layer needs an unset password | misconfig | `capture.py:612-623` |
| Doctor reports secret as set/unset boolean, never value | secret leakage into reports | `doctor.py:152-164` |
| Captured sessions chmod 0700 | R4 | `capture.py:654-658` |
| Export excludes raw drain/perf/stderr, redacts cmdline/exe and home prefix | R4 | `cli.py:222-246,249-313` |
| Import zip-slip guard, exclusive-create claim, post-import audit | R5 | `cli.py:316-357` |
| Crash-safe atomic writes + directory fsync | evidence durability | `io.py:11-43` |
| Prune trash grace window (`APM_PRUNE_GRACE_HOURS`) | accidental destruction | `session.py`, `docs/APM.md` |
| Bridge endpoints admin-only per verb (`Apm` GET-only), allowlisted toggles, locked RMW | R1 scope limit | `WebApi.cs:37-41,133-154,196-238` |
| Server-controlled telnet noise discarded before persistence | PII in store | `app_scrape.py:41-49` |
| Integrity manifests verified at finalize/import | R6 (partial) | `finalize.py`, `session.py` |

## Gaps (ranked; fixes belong to sec-review)

- **G1 (doc drift / false claim):** `docs/APM.md:106` states sessions are
  chmod 0700 without qualification; imported sessions are not chmod'ed
  (`cli.py:346-347`). Corrected wording applied to APM.md in the same pass
  that created this file; code fix (chmod at import) belongs to sec-review.
- **G2:** `--telnet-password` argv options exist despite env-only guidance
  (`README.md:203`, `AGENTS.md` rule 4); secret lands in shell history and
  `/proc` cmdline of the CLI itself (`cli.py:120-124,806-808,938-940`).
  Candidate fix: drop the options or print a deprecation warning.
- **G3:** No shipped sudoers policy constrains the `sudo -n` surface the
  collectors require (`Makefile:23`, `scripts/check_bt.sh:20-21` document the
  dependency only). Anyone with passwordless sudo plus this repo can profile
  any pid (R3).
- **G4:** Import lacks decompression size/member limits (R5, local DoS only).
- **G5:** No SECURITY.md, so no disclosure contact, supported-version
  statement, or vulnerability-handling path exists anywhere in the repo.
- **G6:** Telnet authentication trusts whatever answers the port; no TOFU or
  host identity pinning is possible over plaintext telnet (inherent to the
  game interface; noted so nobody claims otherwise).

## Response readiness (note only)

- Forensic trail: each capture writes versioned metadata, collector results,
  and hash manifests under the session dir (`meta.json`, `finalize.py`),
  giving an investigator per-artifact integrity checks; the bridge logs to
  the game log via `Log.Out` (`BridgeMod.cs:253`). o11y-review owns log
  structure; no central audit of CLI invocations exists (who ran what is not
  recorded anywhere).
- Vulnerability-to-fix path: undocumented (G5).

## Related

- Capture lifecycle and validity: `docs/APM.md`
- Bridge schema and overhead controls: `bridge/README.md`
