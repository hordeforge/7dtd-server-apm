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

        static bool? ReadEnabled()
        {
            try
            {
                if (!File.Exists(ConfigPath)) return null;
                return (bool?)JObject.Parse(File.ReadAllText(ConfigPath))["Enabled"];
            }
            catch (Exception ex) { BridgeMod.Log("perf state read failed: " + ex.Message); return null; }
        }

        static bool WriteEnabled(bool enabled)
        {
            try
            {
                JObject root = JObject.Parse(File.ReadAllText(ConfigPath));
                root["Enabled"] = enabled;
                File.WriteAllText(ConfigPath, root.ToString(Newtonsoft.Json.Formatting.Indented));
                BridgeMod.Log("perf mod -> " + (enabled ? "on" : "off") + " (" + ConfigPath + ")");
                return true;
            }
            catch (Exception ex) { BridgeMod.Log("perf state write failed: " + ex.Message); return false; }
        }

        public override void HandleRestGet(RequestContext context)
        {
            PrepareEnvelopedResult(out JsonWriter writer);
            bool? enabled = ReadEnabled();
            var payload = Encoding.UTF8.GetBytes(Newtonsoft.Json.JsonConvert.SerializeObject(
                new { enabled = enabled ?? false, available = enabled.HasValue, path = ConfigPath }));
            writer.WriteRaw(payload);
            SendEnvelopedResult(context, ref writer, HttpStatusCode.OK, null, null, null);
        }

        public override void HandleRestPost(RequestContext context, IDictionary<string, object> _jsonInput, byte[] _jsonInputData)
        {
            PrepareEnvelopedResult(out JsonWriter writer);
            bool target;
            if (_jsonInput == null || !TryGetBool(_jsonInput, "enabled", out target))
            {
                SendEmptyResponse(context, HttpStatusCode.BadRequest, null, "INVALID_BODY", null);
                return;
            }
            if (!WriteEnabled(target))
            {
                SendEmptyResponse(context, HttpStatusCode.InternalServerError, null, "WRITE_FAILED", null);
                return;
            }
            var payload = Encoding.UTF8.GetBytes(Newtonsoft.Json.JsonConvert.SerializeObject(
                new { enabled = target, restarting = true, note = "server restarts in a moment" }));
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
