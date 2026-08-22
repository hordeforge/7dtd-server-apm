# Changelog

User-facing changes for the two shipped artifacts. They version independently:

| Artifact | Version source | Distributed via |
|---|---|---|
| `seven-dtd-apm` host CLI | `pyproject.toml` = `tools/apm_suite/__init__.py` (gated by `scripts/check_version.py`) | local `uv sync`; printed by `uv run 7dtd-apm --version` |
| `7dtd-apm-bridge` server mod | `ModInfo.xml` = `BridgeMod.cs` const = `bridge/README.md` claim (same gate) | zip from `make package`, named after the newest git tag |

Git tags `vX.Y.Z` mirror the **bridge** version and carry annotated release
notes (`git show v2.2.3`). This de facto policy is inferred from history:
only the bridge has ever been tagged, and its in-file bumps precede each tag.
The CLI package has stayed at 2.1.0 since the initial commit despite ongoing
feature work; treat CLI minor bumps as pending until one lands. Breaking
telemetry-schema or config changes to the bridge are expected to bump its
major version.

## Unreleased

### Host CLI

- Fixed: folded/speedscope/flamegraph loaders crashed on non-finite sample
  weights (`int(inf)`); inf/nan samples are skipped now.
- Changed: `--only` layer aliases resolve through one shared table across
  capture planning, summary scoring, and audit; `io/net` and
  `memory_cache/proc` previously meant different things per stage.
- Fixed: budget, compare, and health scored collected layers through three
  drifting copies of the same logic; they share one helper now.
- Fixed: finalize-time lag diagnosis and the bridge analyzer apply identical
  deep-sample scaling (shared attribute helper).
- jitmap files with undecodable bytes no longer abort finalize; percentage
  shares are rounded instead of truncated.

### Bridge mod

- Build: the TypeScript panel build is self-contained (pinned `npx`
  toolchain); no preinstalled global tsc setup needed.

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
