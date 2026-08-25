using System;
using System.IO;

namespace DtdApmBridge
{
    // Shared temp-file hygiene for the periodic telemetry export (Telemetry)
    // and the jitmap publication (JitMap): both write a unique *.tmp beside
    // the final path and atomically swap it in, and both can strand that temp
    // on a crash between write and swap.
    internal static class TempFiles
    {
        // A live temp exists for milliseconds, so anything older than an hour
        // is garbage by construction; swept best-effort so stranded temps do
        // not accumulate across restarts on a 24/7 host. PruneTimestampedDumps
        // only matches finished dumps and nothing else ever deletes these.
        const double StaleMaxAgeHours = 1.0;

        public static void SweepStale(string pattern, string label)
        {
            try
            {
                foreach (string stale in Directory.GetFiles(BridgeMod.OutputDir, pattern))
                {
                    if ((DateTime.UtcNow - File.GetLastWriteTimeUtc(stale)).TotalHours < StaleMaxAgeHours) continue;
                    try { File.Delete(stale); }
                    catch (Exception ex) { BridgeMod.Log("stale " + label + " delete failed: " + ex.Message); }
                }
            }
            catch (Exception ex) { BridgeMod.Log("stale " + label + " sweep failed: " + ex.Message); }
        }

        // Unique per attempt: a console `apm dump`/`apm jitmap` can race the
        // periodic ThreadPool export.
        public static string NewTempPath(string finalPath)
        {
            return finalPath + "." + Guid.NewGuid().ToString("N").Substring(0, 8) + ".tmp";
        }

        // Swap a fully written temp into place with File.Replace/Move so
        // external consumers (scrapers/dashboards, perf via the /tmp symlink)
        // never observe a does-not-exist or half-written final file.
        public static void Publish(string temp, string finalPath)
        {
            if (File.Exists(finalPath)) File.Replace(temp, finalPath, null);
            else File.Move(temp, finalPath);
        }
    }
}
