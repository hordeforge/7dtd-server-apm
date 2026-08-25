using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;

namespace DtdApmBridge
{
    /// <summary>
    /// Writes perf-&lt;pid&gt;.map into the mod's telemetry directory so Linux
    /// perf resolves managed frames (the capture host symlinks it to
    /// /tmp/perf-&lt;pid&gt;.map before recording).
    /// Unity's embedded Mono ignores MONO_ENV_OPTIONS=--jitmap (a mono_main-only
    /// flag), so we force-JIT the hot game types and export their code addresses.
    /// Run via console `apm jitmap` before a capture window; the JIT burst runs
    /// on the main thread, so never call it mid-measurement.
    /// </summary>
    public static class JitMap
    {
        static readonly string[] HotPrefixes =
        {
            "GameManager", "World", "Chunk", "Entity", "EAI", "Net", "GamePath",
            "SpawnManager", "AIDirector", "DynamicMesh", "VehicleManager",
            "DroneManager", "PowerManager", "WaterSplashCubes", "DecoManager",
            "WorldBlockTicker", "SleeperVolume", "ConnectionManager",
            "ThreadManager", "TrajectorySimulation", "AstarManager",
            "GameTimer", "MultiBlockManager", "QuestEventManager", "GameEventManager",
        };

        // In full mode also map the other managed assemblies the hot path runs
        // through; leaf frames in mscorlib/Unity collections otherwise stay [jit].
        static readonly string[] FullAssemblies =
        {
            "Assembly-CSharp", "Assembly-CSharp-firstpass", "mscorlib", "System",
            "System.Core", "UnityEngine.CoreModule", "LiteNetLib",
        };

        static IEnumerable<Assembly> TargetAssemblies(bool full)
        {
            if (!full)
            {
                yield return typeof(GameManager).Assembly;
                yield break;
            }
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                if (assembly.IsDynamic) continue;
                if (FullAssemblies.Contains(assembly.GetName().Name)) yield return assembly;
            }
        }

        // GetTypes() throws ReflectionTypeLoadException when any type in the
        // assembly fails to load (routine across mscorlib/Unity in full mode);
        // fall back to the types that did load instead of aborting the whole map.
        static Type[] SafeGetTypes(Assembly assembly)
        {
            try { return assembly.GetTypes(); }
            catch (ReflectionTypeLoadException ex)
            {
                return ex.Types.Where(t => t != null).ToArray();
            }
            catch { return Array.Empty<Type>(); }
        }

        // Serializes map publication between concurrent command sources: a
        // telnet client and the local console (or two telnet clients) can run
        // `apm jitmap` at once, and unsynchronized StreamWriters on the same
        // final path truncate each other mid-line while readers (perf via the
        // /tmp symlink, the capture host copy) observe torn symbol tables.
        static readonly object WriteLock = new object();

        static void SweepStaleMapTemps()
        {
            TempFiles.SweepStale("perf-*.map.*.tmp", "jitmap temp");
        }

        public static string Write(bool full = false)
        {
            var entries = new List<KeyValuePair<long, string>>();
            int prepared = 0, skipped = 0;
            foreach (Assembly assembly in TargetAssemblies(full))
            foreach (Type type in SafeGetTypes(assembly))
            {
                if (type.IsGenericTypeDefinition) continue;
                string typeName = type.FullName ?? type.Name;
                if (!full && !HotPrefixes.Any(p => typeName.StartsWith(p, StringComparison.Ordinal))) continue;
                const BindingFlags all = BindingFlags.Public | BindingFlags.NonPublic
                    | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
                foreach (MethodInfo method in type.GetMethods(all))
                {
                    if (method.IsAbstract || method.ContainsGenericParameters) { skipped++; continue; }
                    if ((method.Attributes & MethodAttributes.PinvokeImpl) != 0) { skipped++; continue; }
                    if ((method.GetMethodImplementationFlags() & MethodImplAttributes.InternalCall) != 0)
                    { skipped++; continue; }
                    try
                    {
                        RuntimeHelpers.PrepareMethod(method.MethodHandle);
                        long address = method.MethodHandle.GetFunctionPointer().ToInt64();
                        if (address != 0)
                        {
                            entries.Add(new KeyValuePair<long, string>(
                                address, typeName + "." + method.Name));
                            prepared++;
                        }
                    }
                    catch { skipped++; }
                }
            }
            entries.Sort((a, b) => a.Key.CompareTo(b.Key));
            // perf hardcodes /tmp/perf-<pid>.map, but /tmp is tmpfs (RAM) on
            // typical hosts: the map lives on disk here and the APM capture
            // host symlinks it into /tmp (zero RAM) before recording.
            int pid = Process.GetCurrentProcess().Id;
            Directory.CreateDirectory(BridgeMod.OutputDir);
            string path = Path.Combine(BridgeMod.OutputDir, "perf-" + pid + ".map");
            lock (WriteLock)
            {
                // Maps from previous server pids accumulate (~5-10 MB each in full mode);
                // only the current pid's map is ever useful, so sweep the rest. Under
                // WriteLock with publication below so a concurrent writer cannot delete
                // or truncate the freshly published map mid-read.
                try
                {
                    foreach (string old in Directory.GetFiles(BridgeMod.OutputDir, "perf-*.map"))
                        if (!old.EndsWith("perf-" + pid + ".map", StringComparison.Ordinal))
                            File.Delete(old);
                }
                catch { /* stale-map sweep is best-effort */ }
                SweepStaleMapTemps();
                // Stage under a unique temp and Replace so a reader never sees a
                // half-written map through the /tmp symlink (same pattern as the
                // Telemetry latest.json swap).
                string temp = TempFiles.NewTempPath(path);
                try
                {
                    using (var writer = new StreamWriter(temp, false))
                    {
                        for (int i = 0; i < entries.Count; i++)
                        {
                            // Fill the whole gap to the next symbol: unmapped code between
                            // methods (closed generics, wrappers) then resolves to the nearest
                            // preceding method instead of an anonymous [jit] gap. Cap so a
                            // region boundary cannot swallow megabytes of foreign code.
                            long size = i + 1 < entries.Count
                                ? Math.Min(entries[i + 1].Key - entries[i].Key, 0x100000)
                                : 0x4000;
                            if (size <= 0) size = 0x1000;
                            writer.WriteLine(entries[i].Key.ToString("x") + " " + size.ToString("x") + " "
                                + entries[i].Value.Replace(' ', '_'));
                        }
                    }
                    TempFiles.Publish(temp, path);
                }
                catch
                {
                    try { File.Delete(temp); } catch { }
                    throw;
                }
            }
            return "jitmap " + path + " symbols=" + prepared + " skipped=" + skipped;
        }
    }
}
