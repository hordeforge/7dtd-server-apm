using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using Newtonsoft.Json.Linq;
using Utf8Json;
using Webserver;
using Webserver.WebAPI;

namespace DtdApmBridge
{
    /// <summary>Authenticated GET /api/apm endpoint discovered by the V3 WebAPI scanner.</summary>
    public sealed class Apm : AbsRestApi
    {
        public Apm() : base(null) { }

        public override void HandleRestGet(RequestContext context)
        {
            JsonWriter writer;
            PrepareEnvelopedResult(out writer);
            writer.WriteRaw(Encoding.UTF8.GetBytes(Telemetry.SnapshotJson()));
            SendEnvelopedResult(context, ref writer, HttpStatusCode.OK, null, null, null);
        }

        public override int[] DefaultMethodPermissionLevels()
        {
            // GET is administrator-only by default; all mutating verbs remain disabled by the base handler.
            return new[] { 0, 0, 0, 0, 0 };
        }
    }

    /// <summary>
    /// Admin toggle for the sibling EfficientServer perf mod: GET /api/perf
    /// reports the config state, POST {"enabled":bool} flips it and restarts
    /// the server (the container restart policy boots it with the new config).
    /// This is an ops switch for a sibling mod, not a measurement feature.
    /// </summary>
    public sealed class Perf : AbsRestApi
    {
        public Perf() : base(null) { }

        static string ConfigPath => BridgeMod.Config.PerfModConfigPath;

        // Feature groups with an Enabled flag the web UI can toggle. The
        // SkipOnDedicated subsystems are individual booleans (handled below).
        static readonly string[] GroupKeys =
            { "AiLod", "DynamicMesh", "Gc", "Governor", "TickGuard", "AnimatorLod", "CrowdCollisionLod" };

        // Short human descriptions for the web UI toggle list.
        static readonly Dictionary<string, string> GroupDescriptions = new Dictionary<string, string>
        {
            ["AiLod"] = "Scale AI simulation cost by distance from players (full near, cheap far away)",
            ["DynamicMesh"] = "Generate chunk meshes only near players and land claims",
            ["Gc"] = "GC tuning: skip forced collections, safety collect above a RAM threshold",
            ["Governor"] = "TPS governor: shed background work when ticks run over budget",
            ["TickGuard"] = "Emergency tick shedding at sustained overload",
            ["AnimatorLod"] = "Lower animator update rate for distant entities",
            ["CrowdCollisionLod"] = "Throttle crowd collision resolution",
            ["SkipOnDedicated.DynamicMusicSystem"] = "Skip dynamic music playback (no audio sink on a host)",
            ["SkipOnDedicated.WaterSplashParticles"] = "Skip water splash particles (nothing renders them)",
            ["SkipOnDedicated.EnvironmentAudioUpdates"] = "Skip environment audio updates",
            ["SkipOnDedicated.ClothAndJiggleBoneSimulation"] = "Skip cloth and jiggle-bone simulation",
            ["SkipOnDedicated.ExplosionParticles"] = "Skip explosion particle effects",
            ["SkipOnDedicated.AmbientLightSpectrumUpdates"] = "Skip ambient light spectrum updates",
        };

        // Groups the mod ships disabled: not validated as a default, so flipping
        // them on is experimental. Everything else is the reviewed, safe set.
        static readonly HashSet<string> ExperimentalGroups =
            new HashSet<string> { "TickGuard", "AnimatorLod", "CrowdCollisionLod" };

        static string StatusFor(string name) => ExperimentalGroups.Contains(name) ? "experimental" : "safe";

        // Serializes read-modify-write of the perf config between admin
        // requests: without it two POSTs interleave read-modify-write and lose
        // updates, and a GET can read the file mid-write (truncated JSON ->
        // intermittent available=false).
        static readonly object ConfigFileLock = new object();

        static JObject ReadRoot()
        {
            if (!File.Exists(ConfigPath)) return null;
            try { return JObject.Parse(File.ReadAllText(ConfigPath)); }
            catch (Exception ex) { BridgeMod.Log("perf config read failed: " + ex.Message); return null; }
        }

        static bool WriteRoot(JObject root)
        {
            try
            {
                File.WriteAllText(ConfigPath, root.ToString(Newtonsoft.Json.Formatting.Indented));
                return true;
            }
            catch (Exception ex) { BridgeMod.Log("perf config write failed: " + ex.Message); return false; }
        }

        static List<object> BuildGroups(JObject root)
        {
            var groups = new List<object>();
            if (root == null) return groups;
            string desc(string name)
            {
                string d;
                return GroupDescriptions.TryGetValue(name, out d) ? d : "";
            }
            foreach (var g in GroupKeys)
                if (root[g] is JObject jo)
                    groups.Add(new { name = g, enabled = (bool?)jo["Enabled"] ?? false, description = desc(g), status = StatusFor(g) });
            if (root["SkipOnDedicated"] is JObject sk)
                foreach (var kv in sk)
                    if (kv.Value is JValue jv && jv.Type == JTokenType.Boolean)
                        groups.Add(new { name = "SkipOnDedicated." + kv.Key, enabled = (bool)jv, description = desc("SkipOnDedicated." + kv.Key), status = "safe" });
            return groups;
        }

        static bool SetGroup(JObject root, string group, bool enabled)
        {
            string[] parts = group.Split('.');
            if (parts.Length == 2 && parts[0] == "SkipOnDedicated")
            {
                if (root["SkipOnDedicated"] is JObject sk && sk[parts[1]] != null)
                {
                    sk[parts[1]] = enabled;
                    return true;
                }
                return false;
            }
            if (parts.Length == 1 && Array.IndexOf(GroupKeys, parts[0]) >= 0 && root[parts[0]] is JObject jo)
            {
                jo["Enabled"] = enabled;
                return true;
            }
            return false;
        }

        public override void HandleRestGet(RequestContext context)
        {
            PrepareEnvelopedResult(out JsonWriter writer);
            JObject root;
            lock (ConfigFileLock) root = ReadRoot();
            var payload = Encoding.UTF8.GetBytes(Newtonsoft.Json.JsonConvert.SerializeObject(
                new
                {
                    enabled = root != null ? ((bool?)root["Enabled"] ?? false) : false,
                    available = root != null,
                    path = ConfigPath,
                    groups = BuildGroups(root)
                }));
            writer.WriteRaw(payload);
            SendEnvelopedResult(context, ref writer, HttpStatusCode.OK, null, null, null);
        }

        public override void HandleRestPost(RequestContext context, IDictionary<string, object> _jsonInput, byte[] _jsonInputData)
        {
            PrepareEnvelopedResult(out JsonWriter writer);
            // Parse the raw body so nested objects (the batch groups dict) are
            // read reliably regardless of how the base handler typed them.
            JObject body = null;
            try
            {
                if (_jsonInputData != null && _jsonInputData.Length > 0)
                    body = JObject.Parse(Encoding.UTF8.GetString(_jsonInputData));
            }
            catch { }
            if (body == null)
            {
                SendEmptyResponse(context, HttpStatusCode.BadRequest, null, "INVALID_BODY", null);
                return;
            }
            // Read, validate, mutate, and write under one lock so concurrent
            // admin requests cannot interleave the file RMW; responses are sent
            // after the lock is released.
            string errorCode = null;
            int changed = 0;
            lock (ConfigFileLock)
            {
                JObject root = ReadRoot();
                if (root == null)
                {
                    errorCode = "WRITE_FAILED";
                }
                else
                {
                    // Single top-level toggle: {"enabled": bool}
                    if (body["enabled"] is JValue top && top.Type == JTokenType.Boolean)
                    {
                        root["Enabled"] = (bool)top;
                        changed++;
                    }
                    // Single group toggle: {"group": "...", "enabled": bool}
                    if (errorCode == null && body["group"] is JValue gv && body["enabled"] is JValue ev && ev.Type == JTokenType.Boolean)
                    {
                        if (!SetGroup(root, Convert.ToString(gv), (bool)ev)) errorCode = "INVALID_GROUP";
                        else changed++;
                    }
                    // Batch: {"groups": {"AiLod": false, "SkipOnDedicated.WaterSplashParticles": true, ...}}
                    if (errorCode == null && body["groups"] is JObject batch)
                        foreach (var kv in batch)
                        {
                            if (!(kv.Value is JValue bv) || bv.Type != JTokenType.Boolean) { errorCode = "INVALID_BODY"; break; }
                            if (!SetGroup(root, kv.Key, (bool)bv)) { errorCode = "INVALID_GROUP"; break; }
                            changed++;
                        }
                    if (errorCode == null && changed == 0) errorCode = "INVALID_BODY";
                    if (errorCode == null && !WriteRoot(root)) errorCode = "WRITE_FAILED";
                }
            }
            if (errorCode != null)
            {
                SendEmptyResponse(context,
                    errorCode == "WRITE_FAILED" ? HttpStatusCode.InternalServerError : HttpStatusCode.BadRequest,
                    null, errorCode, null);
                return;
            }
            BridgeMod.Log("perf config applied " + changed + " change(s)");
            var payload = Encoding.UTF8.GetBytes(Newtonsoft.Json.JsonConvert.SerializeObject(
                new { changed = changed, restarting = true, note = "server restarts in a moment" }));
            writer.WriteRaw(payload);
            SendEnvelopedResult(context, ref writer, HttpStatusCode.OK, null, null, null);
            // Flush the response, then shut down; the container restart policy
            // boots the server again with the flipped config.
            System.Threading.Tasks.Task.Run(async () =>
            {
                try
                {
                    await System.Threading.Tasks.Task.Delay(1500);
                    SdtdConsole.Instance.ExecuteSync("shutdown", null);
                }
                catch (Exception ex) { BridgeMod.Log("perf restart failed: " + ex.Message); }
            });
        }

        static bool TryGetBool(IDictionary<string, object> input, string key, out bool value)
        {
            value = false;
            if (!input.TryGetValue(key, out object raw) || raw == null) return false;
            if (raw is bool b) { value = b; return true; }
            if (raw is string s) return bool.TryParse(s, out value);
            if (raw is long l) { value = l != 0; return true; }
            if (raw is double d) { value = d != 0; return true; }
            return false;
        }

        public override int[] DefaultMethodPermissionLevels() => new[] { 0, 0, 0, 0, 0 };
    }
}
