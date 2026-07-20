"""Native/OS frame → C# system → mod API fix catalog for 7DTD V3 dedicated.

This is the bridge between Speedscope/eBPF stacks and Harmony/mod work.
Patterns match substrings in native frames (case-sensitive where useful).
"""

from __future__ import annotations

from typing import Any

# Each rule: when native evidence matches, emit a bridge to C# and fixes.
RULES: list[dict[str, Any]] = [
    {
        "id": "gc_mono",
        "title": "Mono / Boehm GC",
        "native_any": [
            "mono_gc_collect",
            "GC_gcollect",
            "GC_try_to_collect",
            "libmonobdwgc",
            "mono_gc_collect_a_little",
            "sgen_",
        ],
        "prof_sections": [
            "GmUpdate",
            "World.TickEntities",
            "EntityAlive.updateTasks",
            "DynamicMeshManager.Update",
        ],
        "csharp": [
            "System.GC pressure from managed allocations on the main sim thread",
            "Common alloc sites: LINQ in hot loops, string concat in AI, new List per tick",
        ],
        "harmony_targets": [
            "GameManager.gmUpdate",
            "World.TickEntities",
            "EntityAlive.updateTasks",
            "EntityAlive.OnUpdateLive",
            "EAIManager.Update",
        ],
        "mod_apis": [
            "EfficientServer: APM bridge DeepMode → find which section correlates with GC",
            "Harmony: remove allocs in Prefix/Postfix on listed targets",
            "Pool List/Dictionary; avoid string.Format in entity tick",
            "XML: reduce entity count (MaxSpawnedZombies) to cut GC volume",
        ],
    },
    {
        "id": "entity_ai",
        "title": "Entity AI / path follow",
        "native_any": [
            # often only anonymous JIT; rely on section correlation + job names
            "PathFinder",
            "Astar",
            "ASPPath",
            "PathNavigate",
            "EntityMoveHelper",
            "UpdateMoveHelper",
            "FindPath",
        ],
        "prof_sections": [
            "World.TickEntities",
            "EntityAlive.updateTasks",
            "EAIManager.Update",
            "PathNavigate.UpdateNavigation",
            "EntityMoveHelper.UpdateMoveHelper",
            "EntityAlive.FindPath",
            "EAIApproachAndAttackTarget.Update",
            "ASPPathNavigate.GetPathTo",
            "ASPPathFinder.Calculate",
            "ASPPathNavigate.pathFollow",
            "AstarManager.UpdateGraphs",
        ],
        "csharp": [
            "EntityAlive.updateTasks → EAIManager.Update / UAI",
            "GamePath.PathFinderThread / AStar pathfinding",
            "World.TickEntities / TickEntity / EntityActivityUpdate (aiActiveScale)",
        ],
        "harmony_targets": [
            "EntityAlive.updateTasks",
            "EAIManager.Update",
            "World.TickEntities",
            "World.EntityActivityUpdate",
            "World.TickEntity",
        ],
        "mod_apis": [
            "EfficientServer AiLodPatch / UpdateTasksLodPatch (aiActiveScale, far skip)",
            "loadgen shutdown after tests; lower MaxSpawnedZombies in serverconfig",
            "Harmony postfix EntityActivityUpdate for custom LOD",
            "APM bridge DeepMode during horde to confirm EaiManagerUpdate cost",
        ],
    },
    {
        "id": "tick_entities",
        "title": "World entity tick budget",
        "native_any": ["LetBlocksFall", "GroupFallingBlocks", "FallingBlock"],
        "prof_sections": [
            "World.TickEntities",
            "World.TickEntitiesSlice",
            "World.TickEntitiesFlush",
            "World.TickEntity",
            "Entity.OnUpdateEntity",
            "EntityAlive.OnUpdateLive",
            "World.EntityActivityUpdate",
            "World.LetBlocksFall",
            "World.GroupFallingBlocks",
            "World.AddFallingBlock",
            "TrajectorySimulation.UpdateSimulationQueue",
        ],
        "csharp": [
            "World.TickEntities / TickEntitiesSlice / TickEntity",
            "World.EntityActivityUpdate (closest player, cloth/jiggle)",
        ],
        "harmony_targets": [
            "World.TickEntities",
            "World.TickEntitiesSlice",
            "World.TickEntity",
            "World.EntityActivityUpdate",
        ],
        "mod_apis": [
            "EfficientServer: aggressive AI LOD config (FullAiDistSq, FarScale)",
            "Skip cloth/jiggle on dedicated (SkipOnDedicated.ClothAndJiggle)",
            "serverconfig: MaxSpawnedZombies, MaxSpawnedAnimals",
        ],
    },
    {
        "id": "ai_director",
        "title": "AIDirector / hordes",
        "native_any": [],
        "prof_sections": [
            "AIDirector.Tick",
            "AIDirectorBloodMoonComponent.Tick",
            "SpawnManagerBiomes.SpawnUpdate",
        ],
        "csharp": [
            "AIDirector.Tick / ComponentsTick",
            "AIDirectorBloodMoonComponent",
            "SpawnManagerBiomes.SpawnUpdate",
        ],
        "harmony_targets": [
            "AIDirector.Tick",
            "SpawnManagerBiomes.SpawnUpdate",
            "World.OnUpdateTick",
        ],
        "mod_apis": [
            "Console: spawnhorde rate; gamestage XML",
            "Harmony: throttle SpawnUpdate when entity budget high",
            "sibling 7dtd-loadgen scenario only for synthetic load tests",
        ],
    },
    {
        "id": "dynamic_mesh",
        "title": "Dynamic mesh / imposters",
        "native_any": ["DynamicMesh"],
        "prof_sections": ["DynamicMeshManager.Update", "DynamicMeshServer.Update"],
        "csharp": [
            "DynamicMeshManager.Update",
            "DynamicMeshServer.Update",
            "DynamicMeshSettings (OnlyPlayerAreas, MaxRegionLoadMsPerFrame)",
        ],
        "harmony_targets": [
            "DynamicMeshManager.Update",
            "DynamicMeshServer.Update",
        ],
        "mod_apis": [
            "EfficientServer DynamicMeshBudgetPatch (OnlyPlayerAreas, MaxRegionLoadMsPerFrame, MaxActiveSyncs)",
            "serverconfig: MaxQueuedMeshLayers",
            "Disable map tile rendering (EnableMapRendering=false)",
        ],
    },
    {
        "id": "network",
        "title": "Networking / LiteNetLib",
        "native_any": [
            "LiteNetLib",
            "tcp_sendmsg",
            "tcp_recvmsg",
            "Socket",
            "NetworkServer",
            "ConnectionManager",
            "NetEntityDistribution",
        ],
        "prof_sections": [
            "ConnectionManager.Update",
            "ConnectionManager.LateUpdate",
            "NetConnectionSimple.taskSerialize",
            "NetConnectionSimple.FlushSendQueue",
            "NetEntityDistributionEntry.updatePlayerList",
        ],
        "csharp": [
            "ConnectionManager.Update / ProcessPackages",
            "NetPackage* serialization",
            "NetworkServerLiteNetLib",
        ],
        "harmony_targets": [
            "ConnectionManager.Update",
            "ConnectionManager.ProcessPackages",
        ],
        "mod_apis": [
            "Reduce player count / package spam mods",
            "ServerDisabledNetworkProtocols=SteamNetworking when port-forwarded",
            "FakeClients only for net soak tests; sim lag is usually AI not LNL",
        ],
    },
    {
        "id": "unity_jobs",
        "title": "Unity job workers",
        "native_any": [
            "Job.Worker",
            "JobQueue",
            "Background Job",
            "Worker Thread",
        ],
        "prof_sections": ["GmUpdate", "DynamicMeshManager.Update", "World.OnUpdateTick"],
        "csharp": [
            "Unity job system / Burst jobs (native)",
            "Often mesh, physics, or path-adjacent work",
        ],
        "harmony_targets": [
            "DynamicMeshManager.Update",
            "ChunkManager methods",
            "World.OnUpdateTick",
        ],
        "mod_apis": [
            "Lower mesh generation rate (DynamicMesh settings)",
            "MaxQueuedMeshLayers; ServerMaxAllowedViewDistance",
            "Correlate job CPU% in APM threads/ with managed bridge DynamicMesh*",
        ],
    },
    {
        "id": "futex_lock",
        "title": "Lock / futex contention",
        "native_any": [
            "futex",
            "pthread_mutex",
            "pthread_cond",
            "Monitor.Enter",
            "mono_monitor",
            "mono_locks",
            "lock_internal",
        ],
        "prof_sections": [
            "GmUpdate",
            "World.TickEntities",
            "ConnectionManager.Update",
            "DynamicMeshManager.Update",
        ],
        "csharp": [
            "Managed lock (lock/Monitor) or native mutex under Mono/Unity",
            "Often GC STW, thread pool, or net package queues",
        ],
        "harmony_targets": [
            "GameManager.gmUpdate",
            "ConnectionManager.Update",
            "ThreadManager.UpdateMainThreadTasks",
        ],
        "mod_apis": [
            "Host APM: open sync/futex.bt.out waiter stacks",
            "If stacks show mono_gc_* → fix allocations (see gc_mono)",
            "Avoid blocking IO inside Harmony on main thread",
            "Do not hold locks across SpawnEntityInWorld storms generated by a controlled scenario",
        ],
    },
    {
        "id": "blocking_io",
        "title": "Blocking disk I/O",
        "native_any": [
            "vfs_read",
            "vfs_write",
            "vfs_fsync",
            "ext4_",
            "xfs_",
            "nvme_",
            "blk_",
            "__x64_sys_read",
            "__x64_sys_write",
            "pread64",
            "pwrite64",
        ],
        "prof_sections": [
            "GmUpdate",
            "World.OnUpdateTick",
            "WorldBlockTicker.Tick",
            "GameManager.SaveWorld",
            "GameManager.SavePlayerData",
            "ChunkProviderGenerateWorld.SaveRandomChunks",
            "World.SaveWorldState",
            "ChunkManager.DetermineChunksToLoad",
        ],
        "csharp": [
            "Synchronous file IO on sim thread (region/chunk save/load)",
            "World save, prefab, player data, dynamic mesh files",
        ],
        "harmony_targets": [
            "GameManager.gmUpdate",
            "World.Save*",
            "Chunk* save/load paths (version-specific names)",
        ],
        "mod_apis": [
            "Move Saves to local NVMe",
            "Increase save interval / avoid save spam commands",
            "EnableMapRendering=false",
            "Inspect APM io/vfs.bt.out openat paths + slow stacks",
        ],
    },
    {
        "id": "deco_sleepers",
        "title": "Decoration / sleepers / block ticker",
        "native_any": ["DecoManager", "WaterSplashCubes"],
        "prof_sections": [
            "DecoManager.UpdateTick",
            "World.TickSleeperVolumes",
            "WorldBlockTicker.Tick",
            "World.OnUpdateTick",
            "WaterSplashCubes.Update",
        ],
        "csharp": [
            "DecoManager.UpdateTick",
            "World.TickSleeperVolumes / SleeperVolume.Tick",
            "WorldBlockTicker.Tick",
        ],
        "harmony_targets": [
            "DecoManager.UpdateTick",
            "World.TickSleeperVolumes",
            "WorldBlockTicker.Tick",
            "World.OnUpdateTick",
        ],
        "mod_apis": [
            "EfficientServer DedicatedSkipPatch (music/splash/audio)",
            "Harmony: early-out DecoManager when no players nearby",
            "POI/sleeper density via XML if over-spawned",
        ],
    },
    {
        "id": "main_loop",
        "title": "Main game loop",
        "native_any": [
            "PlayerLoop",
            "UnityMain",
            "RuntimeManager",
            "7DaysToDieServe",
        ],
        "prof_sections": [
            "GmUpdate",
            "GameManager.UpdateTick",
            "ThreadManager.UpdateMainThreadTasks",
        ],
        "csharp": [
            "GameManager.Update → gmUpdate",
            "GameManager.UpdateTick / FixedUpdate",
        ],
        "harmony_targets": [
            "GameManager.gmUpdate",
            "GameManager.Update",
            "GameManager.UpdateTick",
        ],
        "mod_apis": [
            "APM bridge snapshot for child section breakdown",
            "APM bridge sections already wrap gmUpdate children",
            "Remove expensive Prefix on gmUpdate from other mods",
        ],
    },
]

# managed bridge section → primary C# types (for reverse lookup)
SECTION_TO_CSHARP: dict[str, dict[str, Any]] = {
    "GmUpdate": {
        "types": ["GameManager"],
        "methods": ["gmUpdate", "Update"],
    },
    "UpdateTick": {
        "types": ["GameManager"],
        "methods": ["UpdateTick"],
    },
    "OnUpdateTick": {
        "types": ["World"],
        "methods": ["OnUpdateTick"],
    },
    "TickEntities": {
        "types": ["World"],
        "methods": ["TickEntities", "TickEntity", "TickEntitiesSlice"],
    },
    "EntityActivity": {
        "types": ["World"],
        "methods": ["EntityActivityUpdate"],
    },
    "TickSleeperVolumes": {
        "types": ["World", "SleeperVolume"],
        "methods": ["TickSleeperVolumes", "Tick"],
    },
    "AiDirectorTick": {
        "types": ["AIDirector"],
        "methods": ["Tick", "ComponentsTick"],
    },
    "ConnectionUpdate": {
        "types": ["ConnectionManager"],
        "methods": ["Update", "ProcessPackages"],
    },
    "ConnectionLateUpdate": {
        "types": ["ConnectionManager"],
        "methods": ["LateUpdate"],
    },
    "DynamicMeshUpdate": {
        "types": ["DynamicMeshManager"],
        "methods": ["Update"],
    },
    "DynamicMeshServer": {
        "types": ["DynamicMeshServer"],
        "methods": ["Update"],
    },
    "SpawnBiomeUpdate": {
        "types": ["SpawnManagerBiomes"],
        "methods": ["SpawnUpdate", "Update"],
    },
    "DecoUpdate": {
        "types": ["DecoManager"],
        "methods": ["UpdateTick"],
    },
    "WorldBlockTicker": {
        "types": ["WorldBlockTicker"],
        "methods": ["Tick"],
    },
    "EntityUpdateTasks": {
        "types": ["EntityAlive"],
        "methods": ["updateTasks", "OnUpdateLive"],
    },
    "EaiManagerUpdate": {
        "types": ["EAIManager", "EAIBase"],
        "methods": ["Update", "OnUpdateTasks"],
    },
    "PathNavigateUpdate": {
        "types": ["GamePath.PathNavigate", "GamePath.PathFinderThread"],
        "methods": ["UpdateNavigation", "FindPath"],
    },
    "EntityMoveHelper.UpdateMoveHelper": {
        "types": ["EntityMoveHelper"],
        "methods": ["UpdateMoveHelper"],
    },
    "EntityAlive.FindPath": {
        "types": ["EntityAlive", "GamePath.PathFinderThread"],
        "methods": ["FindPath"],
    },
    "NetEntityDistributionEntry.updatePlayerList": {
        "types": ["NetEntityDistributionEntry"],
        "methods": ["updatePlayerList"],
    },
    "WaterSplashCubes.Update": {
        "types": ["WaterSplashCubes"],
        "methods": ["Update"],
    },
    "World.LetBlocksFall": {
        "types": ["World"],
        "methods": ["LetBlocksFall", "GroupFallingBlocks"],
    },
    "World.GroupFallingBlocks": {
        "types": ["World"],
        "methods": ["GroupFallingBlocks"],
    },
    "World.TickEntitiesSlice": {
        "types": ["World"],
        "methods": ["TickEntitiesSlice", "TickEntity"],
    },
    "World.TickEntity": {
        "types": ["World"],
        "methods": ["TickEntity"],
    },
    "EntityAlive.OnUpdateLive": {
        "types": ["EntityAlive"],
        "methods": ["OnUpdateLive", "updateTasks"],
    },
    "ThreadManager.UpdateMainThreadTasks": {
        "types": ["ThreadManager"],
        "methods": ["UpdateMainThreadTasks"],
    },
    "ChunkProviderGenerateWorld.SaveRandomChunks": {
        "types": ["ChunkProviderGenerateWorld"],
        "methods": ["SaveRandomChunks"],
    },
    "EAIApproachAndAttackTarget.Update": {
        "types": ["EAIApproachAndAttackTarget"],
        "methods": ["Update"],
    },
    "ASPPathFinder.Calculate": {
        "types": ["GamePath.ASPPathFinder", "GamePath.ASPPathFinderThread"],
        "methods": ["Calculate", "GetPathTo"],
    },
    "ASPPathNavigate.pathFollow": {
        "types": ["GamePath.ASPPathNavigate"],
        "methods": ["pathFollow"],
    },
    "World.AddFallingBlock": {
        "types": ["World"],
        "methods": ["AddFallingBlock"],
    },
}
