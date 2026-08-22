using System;
using System.IO;
using Newtonsoft.Json;

namespace DtdApmBridge
{
    public sealed class BridgeConfig
    {
        public bool Enabled = true;
        public bool DeepMode = false;
        public double SpikeThresholdMs = 50.0;
        public double PeriodicExportSeconds = 30.0;
        public bool LogPeriodicSummary = true;
        public bool LogSpikes = true;
        public int MaxSpikeRecords = 128;
        public int DeepSampleRate = 16;
        // EfficientServer config the /api/perf toggle edits (ops switch for the
        // sibling perf mod; the path is the bind-mounted host copy, which the
        // server entrypoint re-syncs into the game Mods on every start).
        public string PerfModConfigPath = "/mods/EfficientServer/Config/efficientserver.json";

        public static BridgeConfig Load(string path)
        {
            try
            {
                if (File.Exists(path))
                {
                    BridgeConfig config = JsonConvert.DeserializeObject<BridgeConfig>(File.ReadAllText(path))
                        ?? new BridgeConfig();
                    config.MaxSpikeRecords = Math.Max(1, Math.Min(1024, config.MaxSpikeRecords));
                    config.DeepSampleRate = Math.Max(1, Math.Min(10000, config.DeepSampleRate));
                    // 0 disables periodic export; cap the upper end (1h) so a
                    // typo'd huge value can't silently push the next export past
                    // any realistic capture, effectively disabling it.
                    config.PeriodicExportSeconds = Math.Max(0, Math.Min(3600.0, config.PeriodicExportSeconds));
                    // A <=0 threshold marks EVERY frame a spike -> SampleWorld() +
                    // AddSpike every tick, a heavy per-frame cost on the server.
                    config.SpikeThresholdMs = Math.Max(1.0, Math.Min(60000.0, config.SpikeThresholdMs));
                    return config;
                }
            }
            catch (Exception ex) { BridgeMod.Log("config load failed: " + ex.Message); }
            return new BridgeConfig();
        }
    }
}
