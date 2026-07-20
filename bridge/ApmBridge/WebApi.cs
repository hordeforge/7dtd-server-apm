using System;
using System.Net;
using System.Text;
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
}
