using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using HarmonyLib;

namespace DtdApmBridge
{
    public sealed class BridgeMod : IModApi
    {
        public const string Version = "2.1.0";
        static readonly Dictionary<string, string> Status = new Dictionary<string, string>();
        static readonly Dictionary<MethodBase, int> SectionIds = new Dictionary<MethodBase, int>();
        static readonly HashSet<int> DeepIds = new HashSet<int>();
        static Harmony _harmony;
        // volatile: Reload() swaps the reference from the console thread while
        // the game thread reads it every frame; each object is immutable-in-use.
        public static volatile BridgeConfig Config = new BridgeConfig();
        public static string ModDir = "";
        public static string OutputDir => Path.Combine(ModDir, "telemetry");

        public void InitMod(Mod mod)
        {
            ModDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? "";
            Config = BridgeConfig.Load(Path.Combine(ModDir, "Config", "apmbridge.json"));
            if (!Config.Enabled) { Log("disabled by configuration"); return; }
            _harmony = new Harmony("com.7dtd.apm.bridge");
            PatchFrame();
            PatchSections();
            PatchMapTransfers();
            Telemetry.QueueLatest();
            Log("ready version=" + Version + " hooks=" + Status.Count + " output=" + OutputDir);
        }

        static Type GameType(string name) => AccessTools.TypeByName(name);
        static void PatchFrame()
        {
            // Null-check the type first, like the other patch sites (114, 145):
            // if GameManager is absent on this build, mark unavailable instead of
            // risking an NRE that would take down the server at mod load.
            Type gm = GameType("GameManager");
            MethodInfo method = gm == null ? null : AccessTools.Method(gm, "gmUpdate");
            if (method == null) { Status["frame"] = "unavailable"; return; }
            try
            {
                _harmony.Patch(method, new HarmonyMethod(typeof(BridgeMod), nameof(FramePrefix)),
                    new HarmonyMethod(typeof(BridgeMod), nameof(FramePostfix)));
                Status["frame"] = "active:" + Describe(method);
            }
            catch (Exception ex) { Status["frame"] = "failed:" + ex.Message; }
        }
        static void PatchSections()
        {
            string[] normal = {
                "GameManager:UpdateTick", "World:OnUpdateTick", "World:TickEntities",
                "World:EntityActivityUpdate", "World:TickSleeperVolumes", "AIDirector:Tick",
                "ConnectionManager:Update", "ConnectionManager:LateUpdate",
                "DynamicMeshManager:Update", "DynamicMeshServer:Update", "DecoManager:UpdateTick",
                "SpawnManagerBiomes:SpawnUpdate", "WorldBlockTicker:Tick",
                // Expensive, player-visible operations that are easy to miss in frame aggregates.
                "GameManager:ExplosionServer", "GameManager:explode",
                // explode sub-split: block-damage sphere vs entity-damage (OverlapSphere
                // + per-victim LOS raycast) vs the client-visual path (which on a
                // dedicated server still Instantiates the explosion prefab) vs the
                // block-change application. Attributes the blood-moon explode cost.
                "Explosion:AttackBlocks", "Explosion:AttackEntites",
                "GameManager:ExplosionClient", "GameManager:ChangeBlocks",
                "GameManager:SaveWorld", "GameManager:SavePlayerData",
                "NetEntityDistribution:OnUpdateEntities",
                "ChunkManager:SendChunksToClients", "NetPackageChunk:Setup",
                "NetPackageChunk:write", "NetPackageMapChunks:Setup", "NetPackageMapChunks:write",
                "NetPackageWorldFolder:StartSendingPacketsToClient",
                // NOT taskSerialize: it is a long-lived per-connection writer-thread
                // task (runs for the connection's whole lifetime), so prefix/postfix
                // wall-clock timing reports its lifetime (600s+), not per-tick work -
                // it swamped the section table and subsystem attribution. FlushSendQueue
                // is a bounded per-flush call and stays.
                "NetConnectionSimple:FlushSendQueue",
                // Per-tick world systems visible in load-test stacks.
                "WaterSplashCubes:Update", "World:LetBlocksFall", "World:GroupFallingBlocks",
                // Sim hot path: entity work is spread across frames via slices
                // (see research/il/gmUpdate STRUCTURE.md); TickEntities alone
                // only prepares the list.
                "World:TickEntitiesSlice:System.Int32", "World:TickEntitiesFlush",
                "ThreadManager:UpdateMainThreadTasks",
                "TrajectorySimulation:UpdateSimulationQueue",
                "GameManager:ExplodeGroupFrameUpdate",
                // Periodic server IO inside the frame.
                "ChunkManager:DetermineChunksToLoad",
                "ChunkProviderGenerateWorld:SaveRandomChunks", "World:SaveWorldState",
                // Once-per-frame manager updates from gmUpdate phase B.
                "GameEventManager:Update", "QuestEventManager:Update",
                "NavObjectManager:Update", "FactionManager:Update", "TurretTracker:Update",
                "VehicleManager:Update", "DroneManager:Update", "PowerManager:Update",
                // Pathfinding maintenance + blood-moon director (optimizer A/B list).
                "AstarManager:UpdateGraphs", "AIDirectorBloodMoonComponent:Tick",
                "World:AddFallingBlock",
                // Dark-matter attribution: UpdateTick / OnUpdateTick callees that were
                // untimed, to close the parent-minus-children residual at heavy load.
                // (Chunk:UpdateTick is deliberately absent: thousands of calls/tick
                // would make the per-call Stopwatch overhead distort the measurement.)
                "GameStateManager:OnUpdateTick", "ChunkManager:GetActiveChunkSet",
                "ChunkProviderGenerateWorld:MainThreadCacheProtectedPositions",
                "MultiBlockManager:MainThreadUpdate", "World:updateChunksToUncull",
                "World:checkPOIUnculling", "Conductor:Update"
            };
            string[] deep = { "EntityAlive:updateTasks", "EAIManager:Update",
                "GamePath.PathNavigate:UpdateNavigation", "GamePath.ASPPathNavigate:UpdateNavigation",
                // Per-entity hot paths; sampled to respect the record-overhead budget.
                "EntityMoveHelper:UpdateMoveHelper", "EntityAlive:FindPath",
                "NetEntityDistributionEntry:updatePlayerList",
                "World:TickEntity:Entity,System.Single",
                "Entity:OnUpdateEntity", "EntityAlive:OnUpdateLive",
                // Path pipeline: enqueue is EntityAlive:FindPath above; these
                // cover the worker drain and per-path compute (admission
                // experiments need enqueue vs drain vs compute).
                "GamePath.ASPPathNavigate:GetPathTo",
                "GamePath.ASPPathFinder:Calculate",
                "GamePath.ASPPathNavigate:pathFollow",
                // Dominant combat EAI (846 IL, up to 3x FindPath per update).
                "EAIApproachAndAttackTarget:Update",
                // Tile-entity chunk load/save: the mono_alloc churn probe named
                // these as the steady small-object allocation floor (reflection
                // in PooledBinaryWriter). Measure their CPU cost too. Sampled.
                "TileEntity:InstantiateFromRead",
                "TileEntityFeatureData:InstantiateModule" };
            foreach (string spec in normal) PatchSection(spec, false);
            foreach (string spec in deep) if (Config.DeepMode) PatchSection(spec, true);
            foreach (string spec in deep) if (!Config.DeepMode) Status[spec] = "disabled:deep-mode";
        }
        static void PatchMapTransfers()
        {
            foreach (string typeName in new[] { "NetPackageChunk", "NetPackageMapChunks", "NetPackageWorldFolder" })
            {
                Type type = GameType(typeName); MethodInfo setup = type == null ? null : AccessTools.Method(type, "Setup");
                if (setup == null) continue;
                try { _harmony.Patch(setup, postfix: new HarmonyMethod(typeof(BridgeMod), nameof(MapTransferPostfix))); }
                catch (Exception ex) { Status[typeName + ":transfer-counter"] = "failed:" + ex.Message; }
            }
        }
        // GetLength lookup cache: this postfix fires per network package (hundreds/s
        // during joins and steady chunk streaming), so uncached AccessTools.Method
        // reflection per call is real 24/7 overhead. Keyed by concrete package type.
        static readonly System.Collections.Concurrent.ConcurrentDictionary<Type, MethodInfo> GetLengthCache
            = new System.Collections.Concurrent.ConcurrentDictionary<Type, MethodInfo>();

        public static void MapTransferPostfix(MethodBase __originalMethod, object __result)
        {
            try
            {
                object package = __result;
                MethodInfo getLength = package == null ? null
                    : GetLengthCache.GetOrAdd(package.GetType(), t => AccessTools.Method(t, "GetLength"));
                int bytes = getLength == null ? 0 : Convert.ToInt32(getLength.Invoke(package, null));
                Telemetry.RecordTransfer(__originalMethod.DeclaringType.Name, bytes);
            }
            catch (Exception ex) { Log("map transfer counter failed: " + ex.Message); }
        }
        static string Describe(MethodInfo method) => method.DeclaringType.FullName + "." + method.Name
            + "(" + string.Join(",", method.GetParameters().Select(p => p.ParameterType.FullName)) + ")#" + method.MetadataToken;
        static void PatchSection(string spec, bool deep)
        {
            // spec: "Type:Method" or "Type:Method:ParamType1,ParamType2" to pin an overload.
            string[] parts = spec.Split(':'); Type type = GameType(parts[0]);
            Type[] argTypes = null;
            if (parts.Length > 2)
            {
                argTypes = parts[2].Length == 0
                    ? Type.EmptyTypes
                    : Array.ConvertAll(parts[2].Split(','), GameType);
                if (Array.IndexOf(argTypes, null) >= 0) { Status[spec] = "unavailable"; return; }
            }
            MethodInfo method = type == null ? null : AccessTools.Method(type, parts[1], argTypes);
            if (method == null) { Status[spec] = "unavailable"; return; }
            try
            {
                _harmony.Patch(method, new HarmonyMethod(typeof(BridgeMod), nameof(SectionPrefix)),
                    new HarmonyMethod(typeof(BridgeMod), nameof(SectionPostfix)));
                int id = Telemetry.Register(method.DeclaringType.Name + "." + method.Name, deep);
                SectionIds[method] = id;
                if (deep) DeepIds.Add(id);
                Status[spec] = "active:" + Describe(method);
            }
            catch (Exception ex) { Status[spec] = "failed:" + ex.Message; }
        }
        // Instrumentation hooks run inside patched game methods on the main
        // thread. An escaping exception would crash the server tick, so every
        // hook swallows its own failures (losing at most one sample) - a monitor
        // must never take down the process it measures.
        public static void FramePrefix() { try { Telemetry.BeginFrame(); } catch { } }
        public static void FramePostfix() { try { Telemetry.EndFrame(); } catch { } }
        public static void SectionPrefix(MethodBase __originalMethod, out long __state)
        {
            __state = 0;
            try
            {
                if (SectionIds.TryGetValue(__originalMethod, out int id)
                    && Telemetry.ShouldSample(id, DeepIds.Contains(id)))
                    __state = Telemetry.Start();
            }
            catch { __state = 0; }
        }
        public static void SectionPostfix(MethodBase __originalMethod, long __state)
        {
            try
            {
                if (__state != 0 && SectionIds.TryGetValue(__originalMethod, out int id))
                    Telemetry.Record(id, __state);
            }
            catch { }
        }
        static object GameIdentity()
        {
            try
            {
                Assembly assembly = typeof(GameManager).Assembly;
                string path = assembly.Location;
                using (FileStream stream = File.OpenRead(path))
                using (SHA256 hash = SHA256.Create())
                    return new { assembly = assembly.GetName().Name, version = assembly.GetName().Version.ToString(),
                        sha256 = BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant() };
            }
            catch (Exception ex) { return new { error = ex.Message }; }
        }
        public static object Capabilities() => new { hooks = Status.OrderBy(x => x.Key)
            .ToDictionary(x => x.Key, x => x.Value), game = GameIdentity(), deepRequiresRestart = true };
        public static void Reload()
        {
            bool priorDeep = Config.DeepMode;
            Config = BridgeConfig.Load(Path.Combine(ModDir, "Config", "apmbridge.json"));
            if (priorDeep != Config.DeepMode) Log("DeepMode changed; restart required to change installed deep hooks");
        }
        public static void Log(string text) { try { global::Log.Out("[7dtd-apm] " + text); } catch { Console.WriteLine("[7dtd-apm] " + text); } }
    }

    public sealed class ConsoleCmdApm : ConsoleCmdAbstract
    {
        public override string[] getCommands() => new[] { "apm", "apmbridge" };
        public override string getDescription() => "7dtd-apm instrumentation bridge";
        public override string getHelp() => "apm [status|dump|reset|reload|capabilities|jitmap|benchmark [iterations]]";
        public override void Execute(List<string> args, CommandSenderInfo sender)
        {
            string command = args.Count == 0 ? "status" : args[0].ToLowerInvariant();
            try
            {
                if (command == "dump") SdtdConsole.Instance.Output("wrote " + Telemetry.Dump());
                else if (command == "reset") { Telemetry.Reset(); SdtdConsole.Instance.Output("APM reset"); }
                else if (command == "reload") { BridgeMod.Reload(); SdtdConsole.Instance.Output("APM config reloaded"); }
                else if (command == "capabilities") SdtdConsole.Instance.Output(Newtonsoft.Json.JsonConvert.SerializeObject(BridgeMod.Capabilities(), Newtonsoft.Json.Formatting.Indented));
                else if (command == "jitmap")
                    SdtdConsole.Instance.Output(JitMap.Write(args.Count > 1 && args[1] == "full"));
                else if (command == "benchmark") { int n = 100000; if (args.Count > 1) int.TryParse(args[1], out n); SdtdConsole.Instance.Output(Telemetry.Benchmark(n)); }
                else SdtdConsole.Instance.Output(Telemetry.Summary());
            }
            catch (Exception ex) { SdtdConsole.Instance.Output("APM error: " + ex.Message); }
        }
    }
}
