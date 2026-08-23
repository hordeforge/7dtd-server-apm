# Canonical load profile

**Owns:** pinned reference workloads for fair APM / optim A/B.  
**Not:** how APM scores evidence ([APM](APM.md)), bot protocol ([../README](../README.md) in loadgen).

`canonical-heavy-v2` is **the** reference workload for comparing builds and
settings. It is a **heavy** mixed load - it exercises the whole server at once
(network replication + entity-AI + GC) the way a busy population does, not one
subsystem in isolation. A heavy standard is deliberate: the GC/network levers
only separate under real churn (measured: they are a wash at light load). Every
knob is pinned so two runs are comparable.

**Requires `MaxPlayers >= 64`** on the server (`RE_SERVER_MAX_PLAYERS=64`).

## The profile (`canonical-heavy-v2`)

`plans/profile.canonical.json`, one `forensic` capture:

| Knob | Value | Why fixed |
|------|-------|-----------|
| `seed` | `20240717` | deterministic bot action + think-time RNG |
| `clients` | `64` | heavy population; drives the O(players x entities) replication wall |
| `bot_mix` | `traverse:30,wander:20,combat:25,bait:10,demolition:10,chatty:5` | population shape (below) |
| `spawn_entity` | zombies + animals + `zombieDemolition` + vehicles + drone | ambient entity stream |
| `spawn_per_player` / `spawn_every_ms` | `6` / `8000` | dense per-player spawn (~300+ zombies around the cohort) |
| `horde_every_ms` / `horde_waves` | `40000` / `4` | periodic wandering hordes |
| `max_dynamite` | `80` | bounded terrain destruction per demolition bot life |
| `warmup` | `90 s` | load reaches steady state before the window |
| `seconds` | `150` | capture window |
| `preset` | `forensic` | gross-alloc churn + STW pauses + all layers |
| `reset_bridge` | `true` | managed totals cover exactly the window |

## Tier ladder (`plans/profile.tiers.json`)

For scaling analysis and regime coverage, a seed-locked ladder (run with
`apm scenario matrix plans/profile.tiers.json`, then `apm scaling`):

| Tier | clients | zombies | Regime | MaxPlayers |
|------|--------:|---------|--------|-----------:|
| `tier-light` | 16 | light | GC guard helps here | 16 |
| `tier-moderate` | 50 | mixed | former `canonical-v1` | 50 |
| `tier-high-mixed` | 64 | ~300 | = the canonical standard | 64 |
| `tier-player-scale` | 128 | none | O(N^2) network wall (CPU-saturates ~7 FPS) | 128 (`RE_SERVER_MAX_PLAYERS=256`) |

## Reproducible run protocol

Reproducibility needs deterministic inputs **and** a clean starting world. Run:

```bash
# 1. pristine world (deletes the playthrough save; deterministic RWG terrain kept)
../7dtd-loadgen/scripts/reset_world.sh --start     # stops, wipes save, relaunches
#    wait for the server telnet/READY, then:
uv run 7dtd-server-apm scenario matrix plans/profile.canonical.json
```

What makes it reproducible:

- **Fixed world** - RWG seed `botpoi4k` regenerates identical terrain; the
  playthrough save is wiped so block changes/loot/entities from prior runs do
  not carry over.
- **Deterministic bots** - `seed=20240717`; each bot's action RNG is
  `seed + clientId + life`, its think-time jitter RNG a fixed derivative, and
  its mode is assigned by client id. Same cohort behaviour every run.
- **Windowed metrics** - `reset_bridge` + fixed warmup/window make managed
  totals cover the same phase.

Residual variance (bounded, does not move aggregate metrics): join order and
server-side spawn/AI RNG jitter positions slightly; compare the aggregates
(`grossAllocMBPerSecond`, `stw_pause_worst_ms`, `lateTicks`, per-subsystem
`attribution`), not exact per-tick values. **Always `reset_world.sh` between
runs** - demolition permanently mutates terrain, so back-to-back runs without a
reset are not comparable.

## Population mix

Real servers are heterogeneous: most people roam and loot, some fight, some
build/mine, a few chat. The cohort reproduces that with a weighted per-bot mode
mix (`--bot-mix`, assigned deterministically by client id so runs are
repeatable):

| Share | Bot mode | Player behaviour | Server subsystems driven |
|------:|----------|------------------|--------------------------|
| 30% | `traverse` | roam/loot across the map | chunk streaming + tile-entity load/serialization, sleeper spawns |
| 20% | `wander`   | explore near POIs | chunk churn, POI sleeper volumes |
| 25% | `combat`   | fight zombies | AI (`EAIManager`), pathfinding (`AstarVoxelGrid`), damage, death/respawn |
| 10% | `bait`     | hold a spot (base) | stationary AI draw, dense per-tile entity ticking |
| 10% | `demolition` | mine / destroy terrain | falling blocks (`GroupFallingBlocks`/`LetBlocksFall`), water sim, block-ticker, chunk resend |
|  5% | `chatty`   | socialise | chat broadcast, net entity distribution |

Bots move at a real run speed and continuously reconcile position with the
server, so they roam and stream chunks like real clients.

## Ambient spawns (telnet-driven)

Scaled per active player, a mixed entity stream runs alongside the cohort:

- **Zombies** (`zombieBoe/Arlene/Moe`) - core AI + pathfinding load.
- **Animals** - predators (`animalDireWolf`, `animalMountainLion`) and timid prey
  (`animalStag`, `animalBoar`) exercise the non-zombie AI paths; `animalZombieVulture`
  adds flying-entity AI.
- **Exploding entities** (`zombieDemolition`) - detonate on death →
  `ExplosionServer` + falling blocks, without needing a bot to throw dynamite.
- **Vehicles** (`vehicleMotorcycle`, `vehicleTruck4x4`) - `VehicleManager` ticks
  even when idle/undriven.
- **Drone** (`entityJunkDrone`) - `DroneManager` ticks.
- **Wandering hordes** - periodic scout-horde bursts (`--horde-every-ms`) that
  spawn at distance and path in as a group: long-range pathfinding, group
  cohesion, and the spawn manager, distinct from the steady spawn-on-player
  trickle.

## Player actions the bots perform

Bots drive server load through the packets a real client sends. What they do:

| Action | Server effect |
|--------|---------------|
| Roam / run / walk (real ~6 m/s, position reconciled) | chunk streaming, tile-entity load, movement validation |
| Jump / crouch / sneak / strafe / turn / aim | movement + animation state, stamina |
| Detonate dynamite (demolition) | `ExplosionServer`, falling blocks, water sim, chunk resend |
| Melee swing (combat) | damage/health/animation processing |
| Global chat (chatty) | chat broadcast, net distribution |
| Die + respawn | death handling, respawn spawn-point search, entity remove/spawn |

**What bots cannot faithfully do, and why** (documented, not guessed): a bot is
a player *entity* the server tracks by position, but it is not registered as an
*entity observer* - the server streams it no chunk block data and no other-entity
positions (verified: zero `EntitySpawn`/`RelPosAndRot`/`EntityMotion` packets
reach a joined bot). So the bot cannot see zombies to fight specific ones, open a
container it can't locate, or read terrain it isn't sent. And block placement /
tool mining / crafting / vehicle driving need `NetPackageSetBlock` /
block-damage / mount packet formats the codec does not have; reverse-engineering
them risks malformed packets that desync the connection (worse than omitting).

This does **not** weaken the profile's server load: spawned zombies and animals
aggro and path toward bot *positions* server-side, so AI, pathfinding, and the
spawn manager run fully regardless of whether the bot can see or hit them. The
bots supply presence, movement, terrain destruction, and chat; the ambient
stream supplies the entities. Adding true entity/block interaction would require
the bot to participate in the chunk + entity-observer protocol (a large,
desync-risky effort) - tracked as a known gap.

## Subsystem coverage

| Subsystem | Driven by |
|-----------|-----------|
| Chunk streaming / tile-entity serialization | traverse + wander roaming |
| AI + pathfinding | combat/bait bots + spawned zombies/animals + wandering hordes |
| Animal AI | spawned predators/prey/vultures |
| Explosions | `zombieDemolition` + demolition bots' dynamite |
| Falling blocks / water sim | demolition bots + explosions |
| Block updates / chunk resend | demolition terrain damage |
| Vehicles | spawned vehicles |
| Drones | spawned junk drone |
| GC / allocation churn | all of the above (the measured bottleneck) |
| Net distribution / chat | full cohort + chatty bots |

**Not covered automatically: electricity and turrets.** Both are placed-block
subsystems (`PowerManager`, `TurretTracker` only tick when powered blocks and
turrets exist), and bots cannot build or wire. To include them, pre-place a
powered base with auto-turrets near the spawn area (a saved prefab) before the
run; `bait` bots will cluster there and draw spawned zombies into turret range.
Everything else is fully automated.

## Scaling and the wall

`plans/profile.scale-ladder.json` runs the same profile at 25/50/100 players
(same seed) to measure per-player cost and find the tick-budget wall. The steps
hold the profile constant and scale players, keeping entity density per player
roughly constant. Read `grossAllocMBPerSecond`, `stw_pause_worst_ms`,
`lateTicks`, and the per-subsystem `attribution` across steps: per-entity tick
cost is linear (~0.08 ms/entity/tick), so the late-tick and STW curves show
where the tick budget breaks. Note the engine caps active AI (excess spawns get
frozen AI) - the profile intentionally pushes toward that cap to measure the
spawn-manager and freeze behaviour.

## Tuning knobs

Every field is a `scenario run` option, so a one-off variant is a single command:

```bash
uv run 7dtd-server-apm scenario run --preset forensic --clients 60 --warmup 60 \
  --bot-mix "traverse:40,combat:30,demolition:30" \
  --spawn-entity "zombieBoe,animalDireWolf,zombieDemolition" \
  --spawn-per-player 5 --spawn-every-ms 12000 \
  --horde-every-ms 40000 --horde-waves 3 --label my-variant
```

## Related docs

| Doc | Role |
|---|---|
| [APM](APM.md) | Evidence model |
| [FEATURES](FEATURES.md) | CLI surface |
| Loadgen README | [`../../7dtd-loadgen/docs/README.md`](../../7dtd-loadgen/docs/README.md) |
| Measured scaling | [`../../7dtd-server-optimizer/docs/measured-scaling.md`](../../7dtd-server-optimizer/docs/measured-scaling.md) |

## Changelog

- **2026-07-19:** Ownership; related docs.
