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
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const fx = (v, n) => num(v).toFixed(n);
const mib = (bytes) => num(bytes) / 1048576;
// Snapshot shape guards: the API payload may omit sections (partial writes,
// older bridge schema). Coerce once here instead of fallback-defaulting every
// property access in the render path.
function objOrEmpty(candidate) {
    return candidate !== undefined && candidate !== null && typeof candidate === "object" ? candidate : {};
}
function listOrEmpty(candidate) {
    return Array.isArray(candidate) ? candidate : [];
}
function strOrEmpty(candidate) {
    return candidate === undefined || candidate === null ? "" : String(candidate);
}
function grade(update) {
    const avg = num(update.serverTickIntervalAvgMs);
    const tps = avg > 0 ? 1000 / avg : 0;
    const windowUpdates = num(update.windowUpdates);
    const lateShare = windowUpdates > 0 ? num(update.lateTicks) / windowUpdates : 0;
    if (avg === 0) {
        return { tps, cls: "apm-warn", label: "NO DATA" };
    }
    if (tps >= 19 && lateShare < 0.1) {
        return { tps, cls: "apm-ok", label: "HEALTHY" };
    }
    if (tps >= 10) {
        return { tps, cls: "apm-warn", label: "DEGRADED" };
    }
    return { tps, cls: "apm-bad", label: "SATURATED" };
}
// Rising trend: is the tail meaningfully above the head of the window? Used to
// flag GC gen2 / heap climbing (a leak signal) without a server-side history.
function rising(series) {
    if (series.length < 8) {
        return false;
    }
    const half = Math.floor(series.length / 2);
    const head = series.slice(0, half);
    const tail = series.slice(half);
    const headAvg = head.reduce((s, v) => s + v, 0) / head.length;
    const tailAvg = tail.reduce((s, v) => s + v, 0) / tail.length;
    return headAvg > 0 && tailAvg > headAvg * 1.2;
}
function spark(React, values, color, w, h) {
    if (values.length < 2) {
        return null;
    }
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const n = values.length;
    const pts = values
        .map((v, i) => `${(i / (n - 1)) * w},${h - ((v - min) / span) * h}`)
        .join(" ");
    return React.createElement("svg", { className: "apm-spark", width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none" }, React.createElement("polyline", { points: pts, fill: "none", stroke: color, strokeWidth: 1.5 }));
}
function budgetBar(React, frac, cls) {
    const pct = Math.max(0, Math.min(1, frac)) * 100;
    return React.createElement("div", { className: "apm-bar" }, React.createElement("div", { className: `apm-bar-fill ${cls}`, style: { width: `${pct.toFixed(1)}%` } }));
}
// The dashboard HTTP wrapper may hand us the axios response, the {data: ...}
// envelope, or the bare payload; accept all three.
function unwrapSnap(o) {
    if (typeof o !== "object" || o === null) {
        return {};
    }
    const { data } = o;
    if (typeof data !== "object" || data === null) {
        return o;
    }
    const inner = data.data;
    if (typeof inner === "object" && inner !== null) {
        return inner;
    }
    if (data.schema || data.update || data.enabled) {
        return data;
    }
    return o;
}
function pushHistory(hist, utc, live) {
    hist.last = utc;
    const lu = objOrEmpty(live.update);
    const lgc = objOrEmpty(live.gc);
    const avg = num(lu.serverTickIntervalAvgMs);
    const push = (arr, v) => {
        arr.push(v);
        if (arr.length > HIST) {
            arr.shift();
        }
    };
    push(hist.tps, avg > 0 ? 1000 / avg : 0);
    push(hist.alloc, lgc.grossAllocBytesPerSecond >= 0 ? mib(lgc.grossAllocBytesPerSecond) : 0);
    push(hist.gm, num(lu.gmUpdateDurationAvgMs));
    push(hist.gen2, num(lgc.gen2PerSecond));
    push(hist.heap, mib(lgc.heapBytes));
}
function cell(h, label, valueText, cls) {
    return h("div", { className: `apm-cell${cls ? ` ${cls}` : ""}` }, h("span", { className: "apm-label" }, label), h("strong", null, valueText === null || valueText === undefined ? "—" : valueText));
}
function trend(h, React, label, series, cur, color) {
    return h("div", { className: "apm-cell apm-trend" }, h("span", { className: "apm-label" }, label), h("strong", null, cur), spark(React, series, color, 130, 30));
}
function formatUtc(utc) {
    return strOrEmpty(utc).replace("T", " ").replace(/\..*$/u, "");
}
function renderAuthError(h, title, status, authMessage, unavailablePrefix) {
    const msg = status === 403 ? authMessage : `${unavailablePrefix} (HTTP ${status || "error"}).`;
    return h("div", { className: "seven-dtd-apm" }, h("h2", null, title), h("span", { className: "apm-pill apm-bad" }, "AUTH REQUIRED"), h("p", null, msg), h("button", { className: "apm-btn", onClick: () => { location.href = "/"; } }, "Log in"));
}
function renderHead(h, g, frozen, toggleFreeze, copyJson, gc, update) {
    return h("div", { className: "apm-head" }, h("h2", null, "7DTD APM"), h("span", { className: `apm-pill ${g.cls}` }, g.label), h("button", { className: "apm-btn", onClick: toggleFreeze }, frozen ? "▶ Resume" : "⏸ Freeze"), h("button", { className: "apm-btn", onClick: copyJson }, "⧉ Copy JSON"), h("span", { className: "apm-window" }, `window ${fx(gc.windowSeconds, 0)}s · ${num(update.windowUpdates)} ticks${update.deep ? " · deep" : ""}${frozen ? " · FROZEN" : ""}`));
}
function perfToggleLabel(perfBusy, perfEnabled) {
    if (perfBusy) {
        return "restarting server…";
    }
    return perfEnabled ? "Disable (restarts server)" : "Enable (restarts server)";
}
function renderPerfRow(h, perfEnabled, perfAvailable, perfBusy, togglePerf) {
    return h("div", { className: "apm-perf" }, h("span", { className: "apm-label" }, "Performance mod (EfficientServer)"), h("span", { className: `apm-pill ${perfEnabled ? "apm-ok" : "apm-warn"}` }, perfEnabled ? "ENABLED" : "DISABLED"), h("button", { className: "apm-btn", disabled: perfBusy || !perfAvailable, onClick: togglePerf }, perfToggleLabel(perfBusy, perfEnabled)), h("span", { className: "apm-window" }, "flips the config, restarts the server (~1-2 min)"));
}
function renderTickBudget(h, React, update, g) {
    const healthy = g.cls === "apm-ok";
    const frac = num(update.serverTickIntervalAvgMs) / (TICK_BUDGET_MS * 2);
    return h("div", { className: "apm-tick-budget" }, h("span", { className: "apm-label" }, "Tick vs 50 ms budget"), budgetBar(React, frac, healthy ? "" : g.cls), h("span", { className: "apm-budget-val" }, `${fx(update.serverTickIntervalAvgMs, 1)} ms`));
}
function renderGrid(h, React, g, H, update, gc, world, health) {
    const lastAlloc = H.alloc[H.alloc.length - 1];
    return h("div", { className: "apm-grid" }, trend(h, React, "TPS", H.tps, fx(g.tps, 1), "#57d977"), trend(h, React, "Gross alloc MiB/s", H.alloc, fx(lastAlloc === undefined ? 0 : lastAlloc, 1), "#e6bd3a"), trend(h, React, "gmUpdate avg ms", H.gm, fx(update.gmUpdateDurationAvgMs, 2), "#8ab4f8"), cell(h, "Tick max", `${fx(update.serverTickIntervalMaxMs, 1)} ms`, null), cell(h, "gmUpdate max", `${fx(update.gmUpdateDurationMaxMs, 1)} ms`, null), cell(h, "Late ticks", `${num(update.lateTicks)} (${fx(update.tickStallMsTotal, 0)} ms)`, null), cell(h, "Spikes", num(update.totalSpikes), null), cell(h, "Players", `${num(world.players)} / ${num(world.clients)}`, null), cell(h, "Entities", `${num(world.entities)} (${num(world.entityAlives)} AI)`, null), cell(h, "GC gen0/s", fx(gc.gen0PerSecond, 1), null), cell(h, "GC gen2/s", fx(gc.gen2PerSecond, 2), rising(H.gen2) ? "apm-warn" : null), cell(h, "Heap", `${fx(mib(gc.heapBytes), 1)} MiB`, rising(H.heap) ? "apm-warn" : null), cell(h, "Working set", `${fx(mib(world.workingSetBytes), 1)} MiB`, null), cell(h, "Threads", num(world.threadCount), null), cell(h, "Dropped exports", num(health.droppedExports), num(health.droppedExports) > 0 ? "apm-warn" : null));
}
function bySortKey(sort) {
    return (a, b) => {
        const av = sort.key === "name" ? String(a.name) : num(a[sort.key]);
        const bv = sort.key === "name" ? String(b.name) : num(b[sort.key]);
        let cmp = 0;
        if (av < bv) {
            cmp = -1;
        }
        else if (av > bv) {
            cmp = 1;
        }
        return cmp * sort.dir;
    };
}
function sectionRowClass(s) {
    const p95 = num(s.p95Ms);
    if (p95 > 16) {
        return "apm-bad-row";
    }
    if (p95 > 5) {
        return "apm-warn-row";
    }
    return null;
}
function budgetBarClass(frac) {
    if (frac > 0.32) {
        return "apm-bad";
    }
    if (frac > 0.1) {
        return "apm-warn";
    }
    return "";
}
function renderSectionsSection(h, React, sections, sort, setSortKey, filter, setFilter) {
    const shown = [...sections]
        .filter((s) => !filter || strOrEmpty(s.name).toLowerCase().includes(filter.toLowerCase()))
        .sort(bySortKey(sort));
    const th = (label, key) => {
        let marker = "";
        if (sort.key === key) {
            marker = sort.dir < 0 ? " ▼" : " ▲";
        }
        return h("th", { key: label, className: "apm-sortable", onClick: () => setSortKey(key) }, `${label}${marker}`);
    };
    return [
        h("div", { className: "apm-sec-head" }, h("h3", null, "Managed sections"), h("input", {
            className: "apm-filter", type: "text", placeholder: "filter…",
            value: filter, onChange: (e) => setFilter(e.target.value)
        })),
        h("table", { className: "apm-table" }, h("thead", null, h("tr", null, th("Section", "name"), th("Calls", "calls"), th("Avg", "avgMs"), th("P95", "p95Ms"), th("P99", "p99Ms"), th("Max", "maxMs"), h("th", { key: "budget" }, "% of 50ms"))), h("tbody", null, shown.map((s) => {
            const frac = num(s.avgMs) / TICK_BUDGET_MS;
            return h("tr", { key: s.name, className: sectionRowClass(s) }, h("td", null, `${s.name}${s.deep ? " ·deep" : ""}`), h("td", null, num(s.calls)), h("td", null, fx(s.avgMs, 3)), h("td", null, fx(s.p95Ms, 3)), h("td", null, fx(s.p99Ms, 3)), h("td", null, fx(s.maxMs, 3)), h("td", { className: "apm-budget-cell" }, budgetBar(React, frac, budgetBarClass(frac)), h("span", { className: "apm-budget-pct" }, `${fx(frac * 100, 1)}%`)));
        }))),
    ];
}
function renderSpikesSection(h, spikes) {
    if (spikes.length === 0) {
        return null;
    }
    const headers = ["When (UTC)", "gmUpdate ms", "Tick ms", "Players", "Entities"];
    return [
        h("h3", null, "Recent spikes"),
        h("table", { className: "apm-table" }, h("thead", null, h("tr", null, headers.map((x) => h("th", { key: x }, x)))), h("tbody", null, [...spikes].reverse().slice(0, 12).map((s, i) => h("tr", { key: i }, h("td", null, formatUtc(s.utc)), h("td", null, fx(s.gmUpdateDurationMs, 1)), h("td", null, fx(s.serverTickIntervalMs, 1)), h("td", null, num(objOrEmpty(s.world).players)), h("td", null, num(objOrEmpty(s.world).entities)))))),
    ];
}
function renderTransfersSection(h, transfers) {
    const headers = ["Package", "Count", "MiB", "Last bytes", "Max bytes"];
    return [
        h("h3", null, "Map and chunk transfers"),
        h("table", { className: "apm-table" }, h("thead", null, h("tr", null, headers.map((x) => h("th", { key: x }, x)))), h("tbody", null, transfers.map((t) => h("tr", { key: t.name }, h("td", null, t.name), h("td", null, num(t.packages)), h("td", null, fx(t.mebibytes, 2)), h("td", null, num(t.lastBytes)), h("td", null, num(t.maxBytes)))))),
    ];
}
function togglePerfHandler(opts) {
    if (opts.perfBusy || !opts.perfAvailable) {
        return;
    }
    opts.setPerfBusy(true);
    opts.HTTP.post("/api/perf", { enabled: !opts.perfEnabled }).catch(() => {
        opts.setPerfBusy(false);
    });
}
function copySnapshot(snapshot) {
    const txt = JSON.stringify(snapshot, null, 2);
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt);
    }
}
function ApmPanel({ React, HTTP, useQuery }) {
    const h = React.createElement;
    // Authentication gate: an unauthenticated or non-admin session gets a 403
    // from /api/apm. Stop after the first failure instead of polling every 2 s
    // into an error storm (observed when a stale session cookie shows the entry
    // while logged out). retry:false skips react-query's default backoff retries.
    const [authBlocked, setAuthBlocked] = React.useState(false);
    const query = useQuery("seven-dtd-apm", () => HTTP.get("/api/apm"), { refetchInterval: 2000, enabled: !authBlocked, retry: false });
    React.useEffect(() => {
        if (query.isError) {
            setAuthBlocked(true);
        }
    }, [query.isError]);
    const perfQ = useQuery("apm-perf", () => HTTP.get("/api/perf"), { refetchInterval: 30000, enabled: !authBlocked, retry: false });
    const [perfBusy, setPerfBusy] = React.useState(false);
    const hist = React.useRef({ last: null, tps: [], alloc: [], gm: [], gen2: [], heap: [] });
    const [frozen, setFrozen] = React.useState(false);
    const frozenSnap = React.useRef(null);
    const [filter, setFilter] = React.useState("");
    const [sort, setSort] = React.useState({ key: "p95Ms", dir: -1 });
    // All hooks above; a failed fetch (e.g. logged-out session or logged-in
    // non-admin) renders a clear state instead of the NO DATA pills, and the
    // queries are paused (authBlocked) so nothing polls into an error storm.
    if (query.isError) {
        const status = (query.error && query.error.response && query.error.response.status) || 0;
        return renderAuthError(h, "7DTD APM", status, "Authentication required: log in to the dashboard as an admin (permission level 0) to view server telemetry.", "Telemetry unavailable");
    }
    const live = unwrapSnap(query.data);
    const snapshot = frozen && frozenSnap.current ? frozenSnap.current : live;
    if (!frozen && live.utc && live.utc !== hist.current.last) {
        pushHistory(hist.current, live.utc, live);
    }
    const update = objOrEmpty(snapshot.update);
    const health = objOrEmpty(snapshot.health);
    const gc = objOrEmpty(snapshot.gc);
    const world = objOrEmpty(snapshot.world);
    const sections = listOrEmpty(snapshot.sections);
    const transfers = listOrEmpty(snapshot.mapTransfers);
    const spikes = listOrEmpty(snapshot.spikes);
    const g = grade(update);
    const perf = unwrapSnap(perfQ.data);
    const perfEnabled = perf.enabled === true;
    const perfAvailable = perf.available === true;
    const toggleFreeze = () => {
        if (!frozen) {
            frozenSnap.current = live;
        }
        setFrozen(!frozen);
    };
    const togglePerf = () => togglePerfHandler({ HTTP, perfBusy, perfAvailable, setPerfBusy, perfEnabled });
    const copyJson = () => copySnapshot(snapshot);
    const setSortKey = (key) => setSort((s) => ({ key, dir: s.key === key ? -s.dir : -1 }));
    return h("div", { className: "seven-dtd-apm" }, renderHead(h, g, frozen, toggleFreeze, copyJson, gc, update), renderPerfRow(h, perfEnabled, perfAvailable, perfBusy, togglePerf), renderTickBudget(h, React, update, g), renderGrid(h, React, g, hist.current, update, gc, world, health), health.lastExportError ? h("pre", { className: "apm-error" }, health.lastExportError) : null, renderSectionsSection(h, React, sections, sort, setSortKey, filter, setFilter), renderSpikesSection(h, spikes), renderTransfersSection(h, transfers));
}
// Focused panel for the EfficientServer perf mod toggle (its own top-level
// menu entry alongside APM). Same /api/perf admin endpoint.
function EfficiencyPanel({ React, HTTP, useQuery }) {
    const h = React.createElement;
    const [blocked, setBlocked] = React.useState(false);
    const perfQ = useQuery("apm-perf-efficiency", () => HTTP.get("/api/perf"), { refetchInterval: 30000, enabled: !blocked, retry: false });
    React.useEffect(() => {
        if (perfQ.isError) {
            setBlocked(true);
        }
    }, [perfQ.isError]);
    const [busy, setBusy] = React.useState(false);
    if (perfQ.isError) {
        const status = (perfQ.error && perfQ.error.response && perfQ.error.response.status) || 0;
        return renderAuthError(h, "Efficiency", status, "Authentication required: log in to the dashboard as an admin (permission level 0) to control the perf mod.", "Perf API unavailable");
    }
    const perf = unwrapSnap(perfQ.data);
    const enabled = perf.enabled === true;
    const toggle = () => togglePerfHandler({ HTTP, perfBusy: busy, perfAvailable: true, setPerfBusy: setBusy, perfEnabled: enabled });
    return h("div", { className: "seven-dtd-apm" }, h("div", { className: "apm-head" }, h("h2", null, "Efficiency"), h("span", { className: `apm-pill ${enabled ? "apm-ok" : "apm-warn"}` }, enabled ? "ENABLED" : "DISABLED")), h("div", { className: "apm-perf" }, h("span", { className: "apm-label" }, "Performance mod (EfficientServer)"), h("button", { className: "apm-btn", disabled: busy, onClick: toggle }, perfToggleLabel(busy, enabled)), h("span", { className: "apm-window" }, "flips the config, restarts the server (~1-2 min)")));
}
// The stock dashboard renders every webmod `routes` entry as a direct sidebar
// item and every `settings` entry as a tab under Settings, unconditionally.
// Gate the entry on the session cookie so it is hidden while logged out; the
// dashboard reloads the page after login/logout, so this re-evaluates.
const loggedIn = document.cookie.split(";").some((c) => c.trim().startsWith("sid="));
const webMod = {
    about: "Live, low-overhead managed telemetry from 7dtd-apm-bridge.",
    routes: loggedIn ? { "APM": ApmPanel, "Efficiency": EfficiencyPanel } : {},
    settings: {},
    mapComponents: []
};
globalThis[modId] = webMod;
globalThis.dispatchEvent(new Event(`mod:${modId}:ready`));
