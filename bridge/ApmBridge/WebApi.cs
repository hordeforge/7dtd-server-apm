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
            // The body is a live sample, not a representation of a URL: two
            // polls a second apart are different measurements, so a caching
            // proxy in front of the dashboard port must not store a response
            // that carries no freshness of its own and replay it as current.
            // Set before either response is written, so the error envelope is
            // marked as well.
            context.Response.Headers["Cache-Control"] = "no-store";
            // Structured error envelope: a failed snapshot must answer a coded
            // 500, not an unhandled handler exception with no programmatic
            // detail. ApiSnapshotJson counts and times the request and logs the
            // failure with its type and stack trace, which the coded envelope
            // deliberately does not carry.
            string json;
            try { json = Telemetry.ApiSnapshotJson(); }
            catch (Exception)
            {
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
