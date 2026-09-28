using System;
using System.Globalization;
using System.IO;
using Newtonsoft.Json;

namespace DtdApmBridge
{
    /// <summary>Mod settings from Config/apmbridge.json, with every value clamped to a range that works.</summary>
    public sealed class BridgeConfig
    {
        public const double DefaultSpikeThresholdMs = 50.0;
        public const double DefaultPeriodicExportSeconds = 30.0;
        public const int DefaultMaxSpikeRecords = 128;
        public const int DefaultDeepSampleRate = 16;

        // A <=0 threshold marks EVERY frame a spike -> SampleWorld() + AddSpike
        // every tick, a heavy per-frame cost on the server.
        public const double MinSpikeThresholdMs = 1.0;
        public const double MaxSpikeThresholdMs = 60000.0;
        // 0 disables periodic export; cap the upper end (1h) so a typo'd huge
        // value can't silently push the next export past any realistic
        // capture, effectively disabling it.
        public const double MinPeriodicExportSeconds = 0.0;
        public const double MaxPeriodicExportSeconds = 3600.0;
        public const int MinMaxSpikeRecords = 1;
        public const int MaxMaxSpikeRecords = 1024;
        public const int MinDeepSampleRate = 1;
        public const int MaxDeepSampleRate = 10000;

        public bool Enabled = true;
        public bool DeepMode = false;
        public double SpikeThresholdMs = DefaultSpikeThresholdMs;
        public double PeriodicExportSeconds = DefaultPeriodicExportSeconds;
        public bool LogPeriodicSummary = true;
        public bool LogSpikes = true;
        public int MaxSpikeRecords = DefaultMaxSpikeRecords;
        public int DeepSampleRate = DefaultDeepSampleRate;

        /// <summary>Active values after clamping, for the startup log line.</summary>
        public string Describe()
        {
            return string.Format(
                CultureInfo.InvariantCulture,
                "Enabled={0} DeepMode={1} SpikeThresholdMs={2} PeriodicExportSeconds={3} "
                    + "LogPeriodicSummary={4} LogSpikes={5} MaxSpikeRecords={6} DeepSampleRate={7}",
                Enabled, DeepMode, SpikeThresholdMs, PeriodicExportSeconds,
                LogPeriodicSummary, LogSpikes, MaxSpikeRecords, DeepSampleRate);
        }

        internal void Clamp()
        {
            MaxSpikeRecords = Math.Max(MinMaxSpikeRecords, Math.Min(MaxMaxSpikeRecords, MaxSpikeRecords));
            DeepSampleRate = Math.Max(MinDeepSampleRate, Math.Min(MaxDeepSampleRate, DeepSampleRate));
            // Math.Min/Math.Max propagate NaN, and the JSON reader accepts the
            // NaN and Infinity literals, so "SpikeThresholdMs": NaN used to
            // clamp to NaN: every `ms >= SpikeThresholdMs` and
            // `PeriodicExportSeconds > 0` then reads false, and the bridge
            // records no spikes and never arms an export with nothing in the
            // log. A non-finite setting is a typo, not a value: take the default.
            if (double.IsNaN(SpikeThresholdMs) || double.IsInfinity(SpikeThresholdMs))
                SpikeThresholdMs = DefaultSpikeThresholdMs;
            if (double.IsNaN(PeriodicExportSeconds) || double.IsInfinity(PeriodicExportSeconds))
                PeriodicExportSeconds = DefaultPeriodicExportSeconds;
            PeriodicExportSeconds = Math.Max(
                MinPeriodicExportSeconds, Math.Min(MaxPeriodicExportSeconds, PeriodicExportSeconds));
            SpikeThresholdMs = Math.Max(
                MinSpikeThresholdMs, Math.Min(MaxSpikeThresholdMs, SpikeThresholdMs));
        }
    }

    /// <summary>Outcome of one config read: the settings, where they came from, and what failed.</summary>
    public sealed class BridgeConfigLoad
    {
        public BridgeConfig Config = new BridgeConfig();
        /// <summary>Config file read, or null when the mod runs on built-in defaults.</summary>
        public string Source;
        /// <summary>Why the file was rejected, or null when it loaded clean.</summary>
        public string Error;

        /// <summary>One log line naming the source, the failure if any, and the values now in force.</summary>
        public string Describe()
        {
            string head = Source == null
                ? "config: built-in defaults (no Config/apmbridge.json)"
                : "config: " + Source;
            if (Error != null) head += " REJECTED: " + Error + "; running built-in defaults";
            return head + " -> " + Config.Describe();
        }
    }

    public static class BridgeConfigReader
    {
        // Unknown members are an error, not a shrug: a hand-edited config that
        // misspells "DeepMode" (or carries a key from a removed version) would
        // otherwise load as a default and report deep-mode AI/path sections as
        // unavailable with nothing in the log to say why. Comments are still
        // accepted, because Json.NET's reader skips them.
        private static readonly JsonSerializerSettings Settings = new JsonSerializerSettings
        {
            MissingMemberHandling = MissingMemberHandling.Error
        };

        public static BridgeConfigLoad Load(string path)
        {
            BridgeConfigLoad load = new BridgeConfigLoad();
            if (!File.Exists(path)) return load;
            load.Source = path;
            try
            {
                BridgeConfig config = JsonConvert.DeserializeObject<BridgeConfig>(
                    File.ReadAllText(path), Settings);
                if (config == null)
                {
                    load.Error = "file holds JSON null, not a settings object";
                    return load;
                }
                config.Clamp();
                load.Config = config;
            }
            catch (Exception ex)
            {
                // A bad config must not take the server down, but it must not be
                // silent either: the mod stays on built-in defaults and the
                // reason lands in the server log beside the values in force.
                load.Error = ex.Message;
            }
            return load;
        }
    }
}
