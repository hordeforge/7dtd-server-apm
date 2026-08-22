"use strict";
// 7dtd-apm-bridge WebMod (TypeScript source).
// Compiled to bundle.js by `tsc -p WebMod/tsconfig.json` (wired into
// scripts/build_bridge.sh). The dashboard loads /webmods/7dtd-apm-bridge/bundle.js
// and reads window["7dtd-apm-bridge"]: routes render as direct sidebar entries
// (hidden until the sid session cookie is present), settings as Settings tabs.
// Do not hand-edit bundle.js; regenerate from this file.
const modId = "7dtd-apm-bridge";
const HIST = 60; // rolling samples kept for sparklines (~2 min at 2s)
const TICK_BUDGET_MS = 50; // 20 TPS
const num = (v) => (typeof v === "number" && isFinite(v) ? v : 0);
const fx = (v, n) => num(v).toFixed(n);
const mib = (bytes) => num(bytes) / 1048576;
function grade(update) {
    const avg = num(update.serverTickIntervalAvgMs);
    const tps = avg > 0 ? 1000 / avg : 0;
    const windowUpdates = num(update.windowUpdates);
    const lateShare = windowUpdates > 0 ? num(update.lateTicks) / windowUpdates : 0;
    if (avg === 0)
        return { tps, cls: "apm-warn", label: "NO DATA" };
    if (tps >= 19 && lateShare < 0.1)
        return { tps, cls: "apm-ok", label: "HEALTHY" };
    if (tps >= 10)
        return { tps, cls: "apm-warn", label: "DEGRADED" };
    return { tps, cls: "apm-bad", label: "SATURATED" };
}
// Rising trend: is the tail meaningfully above the head of the window? Used to
// flag GC gen2 / heap climbing (a leak signal) without a server-side history.
function rising(series) {
    if (series.length < 8)
        return false;
    const half = Math.floor(series.length / 2);
    const head = series.slice(0, half);
    const tail = series.slice(half);
    const avg = (a) => a.reduce((s, v) => s + v, 0) / a.length;
    const h = avg(head);
    return h > 0 && avg(tail) > h * 1.2;
}
function spark(React, values, color, w, h) {
    if (values.length < 2)
        return null;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const n = values.length;
    const pts = values
        .map((v, i) => `${(i / (n - 1)) * w},${h - ((v - min) / span) * h}`)
        .join(" ");
    return React.createElement("svg", { className: "apm-spark", width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none" }, React.createElement("polyline", { points: pts, fill: "none", stroke: color, strokeWidth: 1.5 }));
}
function bar(React, frac, cls) {
    const pct = Math.max(0, Math.min(1, frac)) * 100;
    return React.createElement("div", { className: "apm-bar" }, React.createElement("div", { className: "apm-bar-fill " + (cls || ""), style: { width: pct.toFixed(1) + "%" } }));
}
function ApmPanel({ React, HTTP, useQuery }) {
    const h = React.createElement;
    const query = useQuery("seven-dtd-apm", async () => HTTP.get("/api/apm"), { refetchInterval: 2000 });
    // The dashboard HTTP wrapper may hand us the axios response, the {data: ...}
    // envelope, or the bare snapshot; accept all three.
    const unwrapSnap = (o) => {
        const s = o && o.data && typeof o.data === "object" ? o.data : null;
        if (s && s.data && typeof s.data === "object")
            return s.data;
        if (s && (s.schema || s.update || s.enabled))
            return s;
        return o || {};
    };
    const hist = React.useRef({ last: null, tps: [], alloc: [], gm: [], gen2: [], heap: [] });
    const [frozen, setFrozen] = React.useState(false);
    const frozenSnap = React.useRef(null);
    const [filter, setFilter] = React.useState("");
    const [sort, setSort] = React.useState({ key: "p95Ms", dir: -1 });
    const perfQ = useQuery("apm-perf", async () => HTTP.get("/api/perf"), { refetchInterval: 30000 });
    const [perfBusy, setPerfBusy] = React.useState(false);
    // All hooks above; a failed fetch (e.g. logged-in non-admin) renders a
    // clear state instead of the NO DATA pills.
    if (query.isError) {
        const status = (query.error && query.error.response && query.error.response.status) || 0;
        const msg = status === 403
            ? "Admin access required: the APM endpoint needs permission level 0."
            : "Telemetry unavailable (HTTP " + (status || "error") + ").";
        return h("div", { className: "seven-dtd-apm" }, h("h2", null, "7DTD APM"), h("span", { className: "apm-pill apm-bad" }, "UNAVAILABLE"), h("p", null, msg));
    }
    const live = unwrapSnap(query.data);
    const data = frozen && frozenSnap.current ? frozenSnap.current : live;
    // Accumulate history on each fresh live sample (dedupe by snapshot utc).
    const lu = live.update || {};
    const lgc = live.gc || {};
    if (!frozen && live.utc && live.utc !== hist.current.last) {
        const H = hist.current;
        H.last = live.utc;
        const avg = num(lu.serverTickIntervalAvgMs);
        const push = (arr, v) => { arr.push(v); if (arr.length > HIST)
            arr.shift(); };
        push(H.tps, avg > 0 ? 1000 / avg : 0);
        push(H.alloc, lgc.grossAllocBytesPerSecond >= 0 ? mib(lgc.grossAllocBytesPerSecond) : 0);
        push(H.gm, num(lu.gmUpdateDurationAvgMs));
        push(H.gen2, num(lgc.gen2PerSecond));
        push(H.heap, mib(lgc.heapBytes));
    }
    const H = hist.current;
    const update = data.update || {};
    const health = data.health || {};
    const gc = data.gc || {};
    const world = data.world || {};
    const sections = data.sections || [];
    const transfers = data.mapTransfers || [];
    const spikes = data.spikes || [];
    const g = grade(update);
    const cell = (label, value, cls) => h("div", { className: "apm-cell" + (cls ? " " + cls : "") }, h("span", { className: "apm-label" }, label), h("strong", null, value == null ? "—" : value));
    const trend = (label, series, cur, color) => h("div", { className: "apm-cell apm-trend" }, h("span", { className: "apm-label" }, label), h("strong", null, cur), spark(React, series, color, 130, 30));
    const toggleFreeze = () => {
        if (!frozen)
            frozenSnap.current = live;
        setFrozen(!frozen);
    };
    const perfSnap = unwrapSnap(perfQ.data);
    const perfEnabled = !!perfSnap.enabled;
    const perfAvailable = !!perfSnap.available;
    const togglePerf = async () => {
        if (perfBusy || !perfAvailable)
            return;
        setPerfBusy(true);
        try {
            await HTTP.post("/api/perf", { enabled: !perfEnabled });
        }
        catch (e) {
            setPerfBusy(false);
        }
    };
    const copyJson = () => {
        const txt = JSON.stringify(data, null, 2);
        if (navigator.clipboard && navigator.clipboard.writeText)
            navigator.clipboard.writeText(txt);
    };
    const setSortKey = (key) => setSort((s) => ({ key, dir: s.key === key ? -s.dir : -1 }));
    const shown = sections
        .filter((s) => !filter || (s.name || "").toLowerCase().includes(filter.toLowerCase()))
        .slice()
        .sort((a, b) => {
        const av = sort.key === "name" ? String(a.name) : num(a[sort.key]);
        const bv = sort.key === "name" ? String(b.name) : num(b[sort.key]);
        return (av < bv ? -1 : av > bv ? 1 : 0) * sort.dir;
    });
    const th = (label, key) => h("th", { key: label, className: "apm-sortable", onClick: () => setSortKey(key) }, label + (sort.key === key ? (sort.dir < 0 ? " ▼" : " ▲") : ""));
    return h("div", { className: "seven-dtd-apm" }, h("div", { className: "apm-head" }, h("h2", null, "7DTD APM"), h("span", { className: "apm-pill " + g.cls }, g.label), h("button", { className: "apm-btn", onClick: toggleFreeze }, frozen ? "▶ Resume" : "⏸ Freeze"), h("button", { className: "apm-btn", onClick: copyJson }, "⧉ Copy JSON"), h("span", { className: "apm-window" }, "window " + fx(gc.windowSeconds, 0) + "s · " + num(update.windowUpdates) + " ticks" +
        (update.deep ? " · deep" : "") + (frozen ? " · FROZEN" : ""))), h("div", { className: "apm-perf" }, h("span", { className: "apm-label" }, "Performance mod (EfficientServer)"), h("span", { className: "apm-pill " + (perfEnabled ? "apm-ok" : "apm-warn") }, perfEnabled ? "ENABLED" : "DISABLED"), h("button", { className: "apm-btn", disabled: perfBusy || !perfAvailable, onClick: togglePerf }, perfBusy ? "restarting server…" : (perfEnabled ? "Disable (restarts server)" : "Enable (restarts server)")), h("span", { className: "apm-window" }, "flips the config, restarts the server (~1-2 min)")), h("div", { className: "apm-tick-budget" }, h("span", { className: "apm-label" }, "Tick vs 50 ms budget"), bar(React, num(update.serverTickIntervalAvgMs) / (TICK_BUDGET_MS * 2), g.cls === "apm-ok" ? "" : g.cls), h("span", { className: "apm-budget-val" }, fx(update.serverTickIntervalAvgMs, 1) + " ms")), h("div", { className: "apm-grid" }, trend("TPS", H.tps, fx(g.tps, 1), "#57d977"), trend("Gross alloc MiB/s", H.alloc, fx(H.alloc[H.alloc.length - 1] || 0, 1), "#e6bd3a"), trend("gmUpdate avg ms", H.gm, fx(update.gmUpdateDurationAvgMs, 2), "#8ab4f8"), cell("Tick max", fx(update.serverTickIntervalMaxMs, 1) + " ms", null), cell("gmUpdate max", fx(update.gmUpdateDurationMaxMs, 1) + " ms", null), cell("Late ticks", num(update.lateTicks) + " (" + fx(update.tickStallMsTotal, 0) + " ms)", null), cell("Spikes", num(update.totalSpikes), null), cell("Players", num(world.players) + " / " + num(world.clients), null), cell("Entities", num(world.entities) + " (" + num(world.entityAlives) + " AI)", null), cell("GC gen0/s", fx(gc.gen0PerSecond, 1), null), cell("GC gen2/s", fx(gc.gen2PerSecond, 2), rising(H.gen2) ? "apm-warn" : null), cell("Heap", fx(mib(gc.heapBytes), 1) + " MiB", rising(H.heap) ? "apm-warn" : null), cell("Working set", fx(mib(world.workingSetBytes), 1) + " MiB", null), cell("Threads", num(world.threadCount), null), cell("Dropped exports", num(health.droppedExports), num(health.droppedExports) > 0 ? "apm-warn" : null)), health.lastExportError ? h("pre", { className: "apm-error" }, health.lastExportError) : null, h("div", { className: "apm-sec-head" }, h("h3", null, "Managed sections"), h("input", {
        className: "apm-filter", type: "text", placeholder: "filter…",
        value: filter, onChange: (e) => setFilter(e.target.value)
    })), h("table", { className: "apm-table" }, h("thead", null, h("tr", null, th("Section", "name"), th("Calls", "calls"), th("Avg", "avgMs"), th("P95", "p95Ms"), th("P99", "p99Ms"), th("Max", "maxMs"), h("th", { key: "budget" }, "% of 50ms"))), h("tbody", null, shown.map((s) => {
        const frac = num(s.avgMs) / TICK_BUDGET_MS;
        const rowCls = num(s.p95Ms) > 16 ? "apm-bad-row" : (num(s.p95Ms) > 5 ? "apm-warn-row" : null);
        return h("tr", { key: s.name, className: rowCls }, h("td", null, s.name + (s.deep ? " ·deep" : "")), h("td", null, num(s.calls)), h("td", null, fx(s.avgMs, 3)), h("td", null, fx(s.p95Ms, 3)), h("td", null, fx(s.p99Ms, 3)), h("td", null, fx(s.maxMs, 3)), h("td", { className: "apm-budget-cell" }, bar(React, frac, frac > 0.32 ? "apm-bad" : (frac > 0.1 ? "apm-warn" : "")), h("span", { className: "apm-budget-pct" }, fx(frac * 100, 1) + "%")));
    }))), spikes.length ? h("h3", null, "Recent spikes") : null, spikes.length ? h("table", { className: "apm-table" }, h("thead", null, h("tr", null, ["When (UTC)", "gmUpdate ms", "Tick ms", "Players", "Entities"].map((x) => h("th", { key: x }, x)))), h("tbody", null, spikes.slice().reverse().slice(0, 12).map((s, i) => h("tr", { key: i }, h("td", null, (s.utc || "").replace("T", " ").replace(/\..*$/, "")), h("td", null, fx(s.gmUpdateDurationMs, 1)), h("td", null, fx(s.serverTickIntervalMs, 1)), h("td", null, num((s.world || {}).players)), h("td", null, num((s.world || {}).entities)))))) : null, h("h3", null, "Map and chunk transfers"), h("table", { className: "apm-table" }, h("thead", null, h("tr", null, ["Package", "Count", "MiB", "Last bytes", "Max bytes"].map((x) => h("th", { key: x }, x)))), h("tbody", null, transfers.map((t) => h("tr", { key: t.name }, h("td", null, t.name), h("td", null, num(t.packages)), h("td", null, fx(t.mebibytes, 2)), h("td", null, num(t.lastBytes)), h("td", null, num(t.maxBytes)))))));
}
// The stock dashboard renders every webmod `routes` entry as a direct sidebar
// item and every `settings` entry as a tab under Settings, unconditionally.
// Gate the entry on the session cookie so it is hidden while logged out; the
// dashboard reloads the page after login/logout, so this re-evaluates.
const loggedIn = document.cookie.split(";").some((c) => c.trim().startsWith("sid="));
const webMod = {
    about: "Live, low-overhead managed telemetry from 7dtd-apm-bridge.",
    routes: loggedIn ? { "APM": ApmPanel } : {},
    settings: {},
    mapComponents: []
};
window[modId] = webMod;
window.dispatchEvent(new Event(`mod:${modId}:ready`));
