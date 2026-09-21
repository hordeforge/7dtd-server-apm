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
            // Structured error envelope: a failed snapshot must answer a coded
            // 500, not an unhandled handler exception with no programmatic
            // detail.
            string json;
            try { json = Telemetry.SnapshotJson(); }
            catch (Exception ex)
            {
                BridgeMod.Log("apm snapshot failed: " + ex.Message);
                SendEmptyResponse(context, HttpStatusCode.InternalServerError, null, "SNAPSHOT_FAILED", null);
                return;
            }
            JsonWriter writer;
            PrepareEnvelopedResult(out writer);
            writer.WriteRaw(Encoding.UTF8.GetBytes(json));
            SendEnvelopedResult(context, ref writer, HttpStatusCode.OK, null, null, null);
        }

        public override int[] DefaultMethodPermissionLevels()
        {
            // GET is administrator-only by default; all mutating verbs remain disabled by the base handler.
            return new[] { 0, 0, 0, 0, 0 };
        }
    }
}
