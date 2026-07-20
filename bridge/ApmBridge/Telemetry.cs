using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using Newtonsoft.Json;

namespace DtdApmBridge
{
    public sealed class Metric
    {
        const int RingSize = 512;
        readonly double[] _ring = new double[RingSize];
        int _write, _count;
        public readonly string Name;
        public long Calls;
        public double TotalMs, MaxMs, LastMs;
        public Metric(string name) { Name = name; }
        public void Add(double ms)
        {
            Calls++; TotalMs += ms; LastMs = ms; if (ms > MaxMs) MaxMs = ms;
            _ring[_write] = ms; _write = (_write + 1) % RingSize; if (_count < RingSize) _count++;
        }
        double Percentile(double p)
        {
            if (_count == 0) return 0;
            var values = new double[_count]; Array.Copy(_ring, values, _count); Array.Sort(values);
            return values[(int)Math.Min(values.Length - 1, Math.Round((p / 100.0) * (values.Length - 1)))];
        }
        public object Snapshot(bool deep) => new {
            name = Name, calls = Calls, avgMs = Calls == 0 ? 0 : TotalMs / Calls,
            lastMs = LastMs, maxMs = MaxMs, p50Ms = Percentile(50), p95Ms = Percentile(95),
            p99Ms = Percentile(99), totalMs = TotalMs,
            // Sampled 1-in-DeepSampleRate; scale calls/totalMs by the rate for
            // attribution. Per-call stats (avg/percentiles) are unbiased.
            deep
        };
        public void Reset() { Calls = 0; TotalMs = MaxMs = LastMs = 0; _write = _count = 0; }
    }

    public sealed class WorldSample
    {
        public string utc;
        public int clients, entities, players, entityAlives, threadCount, unityFrame;
        public long managedBytes, workingSetBytes;
        public int gcGen0, gcGen1, gcGen2;
        public double unityDeltaMs;
    }

    public sealed class SpikeSample
    {
        public string utc;
        public double gmUpdateDurationMs, serverTickIntervalMs;
        public WorldSample world;
    }

    public static class Telemetry
    {
        sealed class TransferCounter
        {
            public long Packages, Bytes;
            public int LastBytes, MaxBytes;
            public object Snapshot(string name) => new { name, packages = Packages, bytes = Bytes,
                mebibytes = Bytes / 1048576.0, lastBytes = LastBytes, maxBytes = MaxBytes };
        }
        const int MaxSections = 128;
        static readonly object Gate = new object();
        static readonly Metric[] Metrics = new Metric[MaxSections];
        static readonly bool[] Deep = new bool[MaxSections];
        static int _metricCount, _sampleSequence;
        static SpikeSample[] _spikes = new SpikeSample[128];
        static int _spikeWrite, _spikeCount;
        static long _updateStart, _previousUpdateStart, _updates, _updateSpikes, _droppedExports;
        static long _lateTicks;
        static double _tickStallMs;
        // GC churn baseline captured at window start (Reset / --reset-bridge) so
        // allocation pressure is measured over the capture window, not uptime.
        static int _gc0Base, _gc1Base, _gc2Base;
        static long _heapBase, _allocBase;
        static double _windowStartRealtime;
        static double _lastRealtime;  // last main-thread Time.realtimeSinceStartup, read by off-thread GcWindow
        // Monotonic GROSS allocation counter. Net heap delta reads ~0 at steady
        // state even under heavy churn; gross is the real pressure driving full
        // GCs. Boehm exports GC_get_total_bytes natively (cheap, one call per
        // export, no uprobe needed) - preferred. GC.GetTotalAllocatedBytes
        // (netstandard2.1) is the managed fallback; both absent -> -1.
        [DllImport("monobdwgc-2.0", EntryPoint = "GC_get_total_bytes")]
        static extern UIntPtr GC_get_total_bytes();

        static readonly MethodInfo _totalAllocated =
            typeof(GC).GetMethod("GetTotalAllocatedBytes", new[] { typeof(bool) });
        static bool _boehmTotalOk = true;
        static long TotalAllocatedBytes()
        {
            if (_boehmTotalOk)
            {
                try { return (long)(ulong)GC_get_total_bytes(); }
                catch { _boehmTotalOk = false; }
            }
            return _totalAllocated == null
                ? -1L
                : (long)_totalAllocated.Invoke(null, new object[] { false });
        }
        static double _updateTotal, _updateMax, _lastUpdate, _tickTotal, _tickMax, _lastTick, _nextExport;
        static WorldSample _lastWorld = new WorldSample { utc = "unavailable" };
        static int _exportQueued;
        // Written on the main thread (SampleWorld, outside Gate) and the export
        // ThreadPool thread (Write/catch), read under Gate in Snapshot. Locking
        // only the read gives no ordering; volatile provides the acquire/release
        // barrier (reference assignment is already atomic, so no torn value).
        static volatile string _lastExportError = "";
        static readonly Dictionary<string, TransferCounter> Transfers = new Dictionary<string, TransferCounter>();

        public static int Register(string name, bool deep)
        {
            lock (Gate)
            {
                if (_metricCount >= MaxSections) throw new InvalidOperationException("APM section capacity exceeded");
                int id = _metricCount++; Metrics[id] = new Metric(name); Deep[id] = deep; return id;
            }
        }
        public static bool ShouldSample(int id, bool deep)
        {
            if (id < 0 || id >= _metricCount) return false;
            if (!deep) return true;
            int sequence = Interlocked.Increment(ref _sampleSequence);
            return sequence % BridgeMod.Config.DeepSampleRate == 0;
        }
        public static long Start() => Stopwatch.GetTimestamp();
        public static void Record(int id, long start)
        {
            double ms = (Stopwatch.GetTimestamp() - start) * 1000.0 / Stopwatch.Frequency;
            // These sections measure per-tick main-thread work; a single invocation
            // is never tens of seconds. A value this large means a long-lived or
            // blocking method (e.g. a worker-thread task) was accidentally patched -
            // drop it so one artifact can't swamp the table and subsystem shares.
            if (ms < 0.0 || ms > 30000.0) return;
            lock (Gate) Metrics[id].Add(ms);
        }
        public static void RecordTransfer(string name, int bytes)
        {
            if (bytes < 0) bytes = 0;
            lock (Gate)
            {
                TransferCounter counter;
                if (!Transfers.TryGetValue(name, out counter)) Transfers[name] = counter = new TransferCounter();
                counter.Packages++; counter.Bytes += bytes; counter.LastBytes = bytes;
                if (bytes > counter.MaxBytes) counter.MaxBytes = bytes;
            }
        }
        public static void BeginFrame()
        {
            // All frame accumulators are written under Gate so the export
            // thread's Snapshot() never observes a half-updated frame.
            long now = Stopwatch.GetTimestamp();
            lock (Gate)
            {
                if (_previousUpdateStart != 0)
                {
                    _lastTick = (now - _previousUpdateStart) * 1000.0 / Stopwatch.Frequency;
                    _tickTotal += _lastTick; if (_lastTick > _tickMax) _tickMax = _lastTick;
                    // 20 TPS budget = 50 ms; anything past 60 ms is a late tick the
                    // player feels. Cumulative overage = "laggy without CPU" gauge.
                    if (_lastTick > 60.0) { _lateTicks++; _tickStallMs += _lastTick - 50.0; }
                }
                _previousUpdateStart = now; _updateStart = now;
            }
        }
        public static void EndFrame()
        {
            if (_updateStart == 0) return;
            double ms = (Stopwatch.GetTimestamp() - _updateStart) * 1000.0 / Stopwatch.Frequency;
            bool spike = ms >= BridgeMod.Config.SpikeThresholdMs;
            double now = UnityEngine.Time.realtimeSinceStartup;
            bool export; double lastTick;
            lock (Gate)
            {
                _lastRealtime = now;  // main-thread timestamp for GcWindow (export runs off-thread)
                _updateStart = 0; _lastUpdate = ms; _updates++; _updateTotal += ms; if (ms > _updateMax) _updateMax = ms;
                if (spike) _updateSpikes++;
                export = BridgeMod.Config.PeriodicExportSeconds > 0 && (_nextExport == 0 || now >= _nextExport);
                if (export) _nextExport = now + BridgeMod.Config.PeriodicExportSeconds;
                lastTick = _lastTick;
            }
            if (!spike && !export) return;
            // SampleWorld touches game state and Process handles; keep it
            // outside Gate so Snapshot() is never blocked behind it.
            WorldSample world = SampleWorld();
            lock (Gate) _lastWorld = world;
            if (spike)
            {
                AddSpike(new SpikeSample { utc = DateTime.UtcNow.ToString("o"), gmUpdateDurationMs = ms,
                    serverTickIntervalMs = lastTick, world = world });
                if (BridgeMod.Config.LogSpikes) BridgeMod.Log("SPIKE gmUpdateDuration=" + ms.ToString("F2") + "ms");
            }
            if (export)
            {
                if (BridgeMod.Config.LogPeriodicSummary) BridgeMod.Log(Summary());
                QueueLatest();
            }
        }
        static void AddSpike(SpikeSample sample)
        {
            lock (Gate)
            {
                int wanted = BridgeMod.Config.MaxSpikeRecords;
                if (_spikes.Length != wanted) { _spikes = new SpikeSample[wanted]; _spikeWrite = _spikeCount = 0; }
                _spikes[_spikeWrite] = sample; _spikeWrite = (_spikeWrite + 1) % _spikes.Length;
                if (_spikeCount < _spikes.Length) _spikeCount++;
            }
        }
        static WorldSample SampleWorld()
        {
            var sample = new WorldSample { utc = DateTime.UtcNow.ToString("o") };
            try { sample.clients = SingletonMonoBehaviour<ConnectionManager>.Instance?.ClientCount() ?? 0; }
            catch (Exception ex) { _lastExportError = "clients: " + ex.Message; }
            try
            {
                World world = GameManager.Instance?.World;
                if (world != null) { sample.entities = world.Entities?.list?.Count ?? 0; sample.players = world.Players?.list?.Count ?? 0; sample.entityAlives = world.EntityAlives?.Count ?? 0; }
            }
            catch (Exception ex) { _lastExportError = "world: " + ex.Message; }
            sample.managedBytes = GC.GetTotalMemory(false); sample.gcGen0 = GC.CollectionCount(0);
            sample.gcGen1 = GC.CollectionCount(1); sample.gcGen2 = GC.CollectionCount(2);
            using (Process process = Process.GetCurrentProcess())
            { sample.workingSetBytes = process.WorkingSet64; sample.threadCount = process.Threads.Count; }
            sample.unityFrame = UnityEngine.Time.frameCount; sample.unityDeltaMs = UnityEngine.Time.unscaledDeltaTime * 1000.0;
            return sample;
        }
        static object Snapshot()
        {
            lock (Gate)
            {
                var spikes = new List<SpikeSample>(_spikeCount);
                for (int i = 0; i < _spikeCount; i++) spikes.Add(_spikes[(_spikeWrite - _spikeCount + i + _spikes.Length) % _spikes.Length]);
                return new {
                    schema = "7dtd.apm.app.v3", provider = "7dtd-apm-bridge", providerVersion = BridgeMod.Version,
                    utc = DateTime.UtcNow.ToString("o"), capabilities = BridgeMod.Capabilities(),
                    measurement = new { updateDurationName = "GameManager.gmUpdate", durationUnit = "ms", deepSampleRate = BridgeMod.Config.DeepSampleRate },
                    update = new { gmUpdateDurationLastMs = _lastUpdate, gmUpdateDurationAvgMs = _updates == 0 ? 0 : _updateTotal / _updates,
                        gmUpdateDurationMaxMs = _updateMax, serverTickIntervalLastMs = _lastTick,
                        serverTickIntervalAvgMs = _updates <= 1 ? 0 : _tickTotal / (_updates - 1), serverTickIntervalMaxMs = _tickMax,
                        lateTicks = _lateTicks, tickStallMsTotal = _tickStallMs,
                        windowUpdates = _updates, totalSpikes = _updateSpikes, deep = BridgeMod.Config.DeepMode },
                    health = new { exportQueued = _exportQueued != 0, droppedExports = _droppedExports, lastExportError = _lastExportError },
                    gc = GcWindow(),
                    world = _lastWorld,
                    mapTransfers = Transfers.OrderBy(x => x.Key).Select(x => x.Value.Snapshot(x.Key)).ToArray(),
                    sections = Metrics.Select((x, i) => i < _metricCount ? x.Snapshot(Deep[i]) : null)
                        .Where(x => x != null).ToArray(), spikes = spikes.ToArray()
                };
            }
        }
        static object GcWindow()
        {
            // Window-relative GC churn: gen0 collections/sec is the allocation
            // pressure that produces the stop-the-world pauses seen as frame
            // spikes. Baseline captured at Reset (window start). Use the cached
            // main-thread timestamp: Snapshot()/GcWindow() run on the export
            // ThreadPool thread and UnityEngine.Time is main-thread-only.
            double elapsed = _lastRealtime - _windowStartRealtime;
            int g0 = GC.CollectionCount(0) - _gc0Base;
            int g1 = GC.CollectionCount(1) - _gc1Base;
            int g2 = GC.CollectionCount(2) - _gc2Base;
            long heapDelta = GC.GetTotalMemory(false) - _heapBase;
            long allocNow = TotalAllocatedBytes();
            long grossAlloc = (allocNow < 0 || _allocBase < 0) ? -1 : allocNow - _allocBase;
            return new {
                windowSeconds = elapsed,
                gen0Collections = g0, gen1Collections = g1, gen2Collections = g2,
                gen0PerSecond = elapsed > 0 ? g0 / elapsed : 0,
                gen2PerSecond = elapsed > 0 ? g2 / elapsed : 0,
                heapDeltaBytes = heapDelta,
                heapBytes = GC.GetTotalMemory(false),
                grossAllocBytes = grossAlloc,
                grossAllocBytesPerSecond = (grossAlloc >= 0 && elapsed > 0) ? grossAlloc / elapsed : -1,
            };
        }
        public static string SnapshotJson() => JsonConvert.SerializeObject(Snapshot());
        static void Write(string path)
        {
            Directory.CreateDirectory(BridgeMod.OutputDir); string temp = path + ".tmp";
            File.WriteAllText(temp, JsonConvert.SerializeObject(Snapshot(), Formatting.Indented));
            if (File.Exists(path)) File.Delete(path); File.Move(temp, path); _lastExportError = "";
        }
        public static void QueueLatest()
        {
            if (Interlocked.CompareExchange(ref _exportQueued, 1, 0) != 0) { Interlocked.Increment(ref _droppedExports); return; }
            ThreadPool.QueueUserWorkItem(_ => { try { Write(Path.Combine(BridgeMod.OutputDir, "apm_app_latest.json")); }
                catch (Exception ex) { _lastExportError = ex.GetType().Name + ": " + ex.Message; BridgeMod.Log("export failed: " + _lastExportError); }
                finally { Interlocked.Exchange(ref _exportQueued, 0); } });
        }
        public static string Dump()
        {
            string path = Path.Combine(BridgeMod.OutputDir, "apm_app_" + DateTime.UtcNow.ToString("yyyyMMdd_HHmmss") + ".json");
            Write(path); Write(Path.Combine(BridgeMod.OutputDir, "apm_app_latest.json")); return path;
        }
        public static string Summary()
        {
            lock (Gate)
                return "APM updates=" + _updates + " gmUpdateAvg=" + (_updates == 0 ? 0 : _updateTotal / _updates).ToString("F2")
                    + "ms tickAvg=" + (_updates <= 1 ? 0 : _tickTotal / (_updates - 1)).ToString("F2") + "ms spikes=" + _updateSpikes + " sections=" + _metricCount;
        }
        public static string Benchmark(int iterations)
        {
            iterations = Math.Max(1000, Math.Min(1000000, iterations));
            var metric = new Metric("benchmark"); var watch = Stopwatch.StartNew();
            for (int i = 0; i < iterations; i++) { long start = Stopwatch.GetTimestamp();
                lock (Gate) metric.Add((Stopwatch.GetTimestamp() - start) * 1000.0 / Stopwatch.Frequency); }
            watch.Stop(); double ns = watch.Elapsed.TotalMilliseconds * 1000000.0 / iterations;
            return "iterations=" + iterations + " nsPerRecord=" + ns.ToString("F1") + " budgetNs=2000 pass=" + (ns <= 2000);
        }
        public static void Reset()
        {
            lock (Gate)
            {
                for (int i = 0; i < _metricCount; i++) Metrics[i].Reset(); Array.Clear(_spikes, 0, _spikes.Length);
                _spikeWrite = _spikeCount = 0; _updateStart = _previousUpdateStart = _updates = _updateSpikes = _droppedExports = 0;
                _updateTotal = _updateMax = _lastUpdate = _tickTotal = _tickMax = _lastTick = _nextExport = 0;
                _lateTicks = 0; _tickStallMs = 0; _lastExportError = "";
                _gc0Base = GC.CollectionCount(0); _gc1Base = GC.CollectionCount(1);
                _gc2Base = GC.CollectionCount(2); _heapBase = GC.GetTotalMemory(false);
                _allocBase = TotalAllocatedBytes();
                _windowStartRealtime = UnityEngine.Time.realtimeSinceStartup;
                Transfers.Clear();
            }
        }
    }
}
