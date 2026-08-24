"use strict";
// 7dtd-server-apm-bridge WebMod (TypeScript source).
// Compiled to bundle.js by `tsc -p WebMod/tsconfig.json` (wired into
// scripts/build_bridge.sh). The dashboard loads /webmods/7dtd-server-apm-bridge/bundle.js
// and reads window["7dtd-server-apm-bridge"]: routes render as direct sidebar entries,
// settings as Settings tabs (both registered unconditionally; the panels
// render their auth-required state when the session lacks admin rights).
// Do not hand-edit bundle.js; regenerate from this file.
//
// The whole body is an IIFE on purpose: webmod bundles are plain <script> tags
// sharing the global scope, and a bare top-level const (e.g. modId) collides
// across mods (SyntaxError kills the later bundle's registration).
(() => {
    const modId = "7dtd-server-apm-bridge";
    const HIST = 60; // rolling samples kept for sparklines (~2 min at 2s)
    const TICK_BUDGET_MS = 50; // 20 TPS
    const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
    const fx = (v, n) => num(v).toFixed(n);
    const mib = (bytes) => num(bytes) / 1048576;
    // Snapshot shape guards: the API payload may omit sections (partial writes,
    // older bridge schema). Coerce once here instead of fallback-defaulting every
    // property access in the render path.
    function objOrEmpty(candidate) {
        if (candidate === undefined || candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) {
            return {};
        }
        // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; SAFETY: the guard above proves the runtime value is a plain object
        return candidate;
    }
    function listOrEmpty(candidate) {
        if (!Array.isArray(candidate)) {
            return [];
        }
        // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; SAFETY: Array.isArray is the runtime proof for the element cast
        return candidate;
    }
    function strOrEmpty(candidate) {
        if (candidate === undefined || candidate === null) {
            return "";
        }
        // oxlint-disable-next-line typescript/no-base-to-string -- deliberate: payload values are JSON primitives (numbers, strings); String() renders them into labels
        return String(candidate);
    }
    function strOr(candidate, fallback) {
        const s = strOrEmpty(candidate);
        return s === "" ? fallback : s;
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
        var _a;
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
        const lastPoint = (_a = pts.split(" ").pop()) !== null && _a !== void 0 ? _a : "";
        const [lastX, lastY] = lastPoint.split(",");
        const gradId = `apm-grad-${color.slice(1)}`;
        // Faint quarter reference lines: unlit structure so position reads without
        // axes even at thumbnail size (preserveAspectRatio none stretches strokes,
        // hence vectorEffect). Decorative overall: the enclosing trend cell prints
        // the current value as text.
        const refs = [0.25, 0.5, 0.75].map((f) => React.createElement("line", {
            key: f, x1: 0, y1: h * f, x2: w, y2: h * f,
            stroke: "rgba(127,127,127,.14)", strokeWidth: 1, vectorEffect: "non-scaling-stroke"
        }));
        return React.createElement("svg", { className: "apm-spark", width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none", "aria-hidden": true }, React.createElement("defs", null, React.createElement("linearGradient", { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, React.createElement("stop", { offset: "0%", stopColor: color, stopOpacity: 0.35 }), React.createElement("stop", { offset: "100%", stopColor: color, stopOpacity: 0.02 }))), ...refs, React.createElement("polygon", { points: `0,${h} ${pts} ${w},${h}`, fill: `url(#${gradId})` }), React.createElement("polyline", { points: pts, fill: "none", stroke: color, strokeWidth: 1.5, vectorEffect: "non-scaling-stroke" }), React.createElement("circle", { cx: lastX, cy: lastY, r: 2, fill: color }));
    }
    function budgetBar(React, frac, cls) {
        const pct = Math.max(0, Math.min(1, frac)) * 100;
        // Decorative: the adjacent .apm-budget-pct span prints the percentage.
        return React.createElement("div", { className: "apm-bar", "aria-hidden": true }, React.createElement("div", { className: `apm-bar-fill ${cls}`, style: { width: `${pct.toFixed(1)}%` } }));
    }
    // The dashboard HTTP wrapper may hand us the axios response, the {data: ...}
    // envelope, or the bare payload; accept all three.
    function unwrapSnap(o) {
        if (typeof o !== "object" || o === null) {
            return {};
        }
        // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; SAFETY: typeof above proves the runtime value is an object
        const record = o;
        const { data } = record;
        if (typeof data !== "object" || data === null) {
            return record;
        }
        // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; SAFETY: typeof above proves the runtime value is an object
        const innerRecord = data;
        const inner = innerRecord.data;
        if (typeof inner === "object" && inner !== null) {
            // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; SAFETY: typeof above proves the runtime value is an object
            return inner;
        }
        if (innerRecord.schema !== undefined || innerRecord.update !== undefined || innerRecord.enabled !== undefined) {
            return innerRecord;
        }
        return record;
    }
    function pushHistory(hist, utc, live) {
        hist.last = utc;
        const lu = objOrEmpty(live.update);
        const lgc = objOrEmpty(live.gc);
        const avg = num(lu.serverTickIntervalAvgMs);
        const push = (arr, v) => {
            arr.push(v);
            if (arr.length > histDepth) {
                arr.shift();
            }
        };
        push(hist.tps, avg > 0 ? 1000 / avg : 0);
        push(hist.alloc, num(lgc.grossAllocBytesPerSecond) >= 0 ? mib(lgc.grossAllocBytesPerSecond) : 0);
        push(hist.gm, num(lu.gmUpdateDurationAvgMs));
        push(hist.gen2, num(lgc.gen2PerSecond));
        push(hist.heap, mib(lgc.heapBytes));
    }
    function cell(h, label, valueText, cls) {
        return h("div", { className: `apm-cell${cls !== null && cls !== "" ? ` ${cls}` : ""}` }, h("span", { className: "apm-label" }, label), h("strong", null, valueText !== null && valueText !== void 0 ? valueText : "n/a"));
    }
    function trend(h, React, label, series, cur, color) {
        return h("div", { className: "apm-cell apm-trend" }, h("span", { className: "apm-label" }, label), h("strong", null, cur), spark(React, series, color, 130, 30));
    }
    function hostStatOf(candidate) {
        if (typeof candidate !== "object" || candidate === null) {
            return null;
        }
        // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; SAFETY: typeof above proves the runtime value is an object
        const o = candidate;
        const memTotal = num(o.memTotalBytes);
        if (memTotal <= 0) {
            return null;
        }
        return {
            load1: num(o.load1),
            load5: num(o.load5),
            load15: num(o.load15),
            memTotalBytes: memTotal,
            memAvailBytes: num(o.memAvailBytes),
            uptimeS: num(o.uptimeS),
            rssBytes: num(o.rssBytes),
            threadCount: num(o.threadCount),
            cpuCores: num(o.cpuCores)
        };
    }
    // Coerce every optional snapshot section once per render instead of guarding
    // each property access in the render path.
    function snapshotViewsOf(snapshot) {
        return {
            update: objOrEmpty(snapshot.update),
            health: objOrEmpty(snapshot.health),
            gc: objOrEmpty(snapshot.gc),
            world: objOrEmpty(snapshot.world),
            host: hostStatOf(snapshot.host),
            sections: listOrEmpty(snapshot.sections),
            transfers: listOrEmpty(snapshot.mapTransfers),
            spikes: listOrEmpty(snapshot.spikes)
        };
    }
    function fmtUptime(uptimeS) {
        const s = Math.floor(uptimeS);
        const d = Math.floor(s / 86400);
        const h = Math.floor((s % 86400) / 3600);
        const m = Math.floor((s % 3600) / 60);
        if (d > 0) {
            return `${d}d ${h}h`;
        }
        if (h > 0) {
            return `${h}h ${m}m`;
        }
        return `${m}m`;
    }
    function renderHostStrip(h, host) {
        const memUsed = Math.max(0, host.memTotalBytes - host.memAvailBytes);
        const memPct = host.memTotalBytes > 0 ? (memUsed / host.memTotalBytes) * 100 : 0;
        return h("div", { className: "apm-host" }, h("span", { className: "apm-label" }, "Host"), cell(h, "Load 1/5/15m", `${host.load1.toFixed(2)} / ${host.load5.toFixed(2)} / ${host.load15.toFixed(2)}`, null), cell(h, "RAM", `${mib(memUsed).toFixed(0)} / ${mib(host.memTotalBytes).toFixed(0)} MiB (${memPct.toFixed(0)}%)`, memPct > 90 ? "apm-bad" : null), cell(h, "RSS", `${mib(host.rssBytes).toFixed(0)} MiB`, null), cell(h, "Threads", `${host.threadCount} / ${host.cpuCores} cores`, null), cell(h, "Uptime", fmtUptime(host.uptimeS), null));
    }
    function formatUtc(utc) {
        return strOrEmpty(utc).replace("T", " ").replace(/\..*$/u, "");
    }
    function renderAuthError(h, title, status, authMessage, unavailablePrefix) {
        const msg = status === 403 ? authMessage : `${unavailablePrefix} (HTTP ${status !== null && status !== void 0 ? status : "error"}).`;
        // The pill must match the message: a network error or 500 is not an auth
        // problem, and telling the user to log in would send them in circles.
        const pill = status === 403 ? "AUTH REQUIRED" : "UNAVAILABLE";
        return h("div", { className: "seven-dtd-apm" }, h("h2", null, title), h("span", { className: "apm-pill apm-bad" }, pill), h("p", null, msg), h("button", { type: "button", className: "apm-btn", onClick: () => { location.href = "/"; } }, "Log in"));
    }
    function renderHead(h, g, frozen, toggleFreeze, copyJson, gc, update) {
        return h("div", { className: "apm-head" }, h("h2", null, "7DTD APM"), h("span", { className: `apm-pill ${g.cls}` }, g.label), 
        // The leading glyphs are decorative; the accessible name is the word only.
        h("button", { type: "button", className: "apm-btn", onClick: toggleFreeze }, h("span", { "aria-hidden": true }, frozen ? "▶ " : "⏸ "), frozen ? "Resume" : "Freeze"), h("button", { type: "button", className: "apm-btn", onClick: copyJson }, h("span", { "aria-hidden": true }, "⧉ "), "Copy JSON"), h("span", { className: "apm-window" }, `window ${fx(gc.windowSeconds, 0)}s · ${num(update.windowUpdates)} ticks${update.deep === true ? " · deep" : ""}${frozen ? " · FROZEN" : ""}`));
    }
    // Two-step confirm for disruptive buttons (same pattern as the Efficiency
    // panel's Apply): the first click arms the button, the second fires it, and
    // arming expires so a stale armed state cannot surprise anyone later.
    const ARMED_WINDOW_MS = 4000;
    // Expiry timers per setter: a re-arm must cancel the previous window instead
    // of letting the stale timer cut the fresh confirm short.
    const armedTimers = new WeakMap();
    function perfToggleLabel(perfBusy, perfEnabled, perfArmed = false) {
        if (perfBusy) {
            return "restarting server…";
        }
        if (perfArmed) {
            return `Confirm ${perfEnabled ? "disable" : "enable"}?`;
        }
        return perfEnabled ? "Disable (restarts server)" : "Enable (restarts server)";
    }
    function armToggle(armed, setArmed, fire) {
        const pending = armedTimers.get(setArmed);
        if (pending !== undefined) {
            clearTimeout(pending);
            armedTimers.delete(setArmed);
        }
        if (armed) {
            setArmed(false);
            fire();
            return;
        }
        setArmed(true);
        armedTimers.set(setArmed, setTimeout(() => {
            armedTimers.delete(setArmed);
            setArmed(false);
        }, ARMED_WINDOW_MS));
    }
    function renderPerfRow(h, perfEnabled, perfAvailable, perfBusy, perfArmed, togglePerf) {
        return h("div", { className: "apm-perf" }, h("span", { className: "apm-label" }, "Performance mod (EfficientServer)"), h("span", { className: `apm-pill ${perfEnabled ? "apm-ok" : "apm-warn"}` }, perfEnabled ? "ENABLED" : "DISABLED"), h("button", {
            type: "button", className: "apm-btn", disabled: perfBusy || !perfAvailable, onClick: togglePerf,
            "aria-label": perfArmed && !perfBusy
                ? `Confirm ${perfEnabled ? "disable" : "enable"} now and restart the server`
                : undefined
        }, perfToggleLabel(perfBusy, perfEnabled, perfArmed)), h("span", { className: "apm-window" }, "flips the config, restarts the server (~1-2 min)"));
    }
    function trendSeriesOf(H) {
        return [
            { key: "tps", label: "TPS", values: H.tps, color: "#57d977", format: (v) => v.toFixed(1) },
            { key: "gm", label: "gmUpdate ms", values: H.gm, color: "#8ab4f8", format: (v) => v.toFixed(2) },
        ];
    }
    function niceMax(value) {
        if (value <= 0) {
            return 10;
        }
        const exp = Math.floor(Math.log10(value));
        const base = Math.pow(10, exp);
        const frac = value / base;
        let nice = 10;
        if (frac <= 1) {
            nice = 1;
        }
        else if (frac <= 2) {
            nice = 2;
        }
        else if (frac <= 5) {
            nice = 5;
        }
        return nice * base;
    }
    // x-axis timescale for the trends chart. Uniform mode gives every sample an
    // equal pixel width. Compressed mode shrinks each step going back by
    // TREND_DECAY, so the recent window keeps full detail while older history
    // tapers toward the left edge; the vertical grid (one line per TREND_GRID_S
    // of real time) bunches up toward the left to visualize the taper.
    const TREND_DECAY = 0.93;
    const TREND_GRID_S = 30;
    const TREND_SAMPLE_S = 2;
    // Configurable history depth (samples; memory stays proportional to what is
    // drawn). Persisted per browser; trimming drops oldest samples first.
    const HISTORY_KEY = "apm.historySamples";
    const HISTORY_CHOICES = [60, 150, 300];
    let histDepth = HIST;
    try {
        const stored = Number(globalThis.localStorage.getItem(HISTORY_KEY));
        if (HISTORY_CHOICES.includes(stored)) {
            histDepth = stored;
        }
        // oxlint-disable-next-line @rikalabs/no-silent-catch-fallback -- deliberate: storage can be blocked (private mode); the default depth still applies for the session
    }
    catch (_a) {
        // Keep the default depth.
    }
    function persistHistDepth(samples) {
        try {
            globalThis.localStorage.setItem(HISTORY_KEY, String(samples));
            // oxlint-disable-next-line @rikalabs/no-silent-catch-fallback -- deliberate: persistence is best-effort; the chosen depth still applies for the session
        }
        catch (_a) {
            // Storage unavailable: keep the session-only depth.
        }
    }
    function trimHistory(hist) {
        const series = [hist.tps, hist.alloc, hist.gm, hist.gen2, hist.heap];
        for (const arr of series) {
            while (arr.length > histDepth) {
                arr.shift();
            }
        }
    }
    // Pixel width of the newest step (samples get narrower going back by decay).
    function trendStep0(innerW, n, compressed) {
        if (!compressed) {
            return innerW / (n - 1);
        }
        return (innerW * (1 - TREND_DECAY)) / (1 - Math.pow(TREND_DECAY, n));
    }
    // x (0..innerW) of the sample that is `age` samples old (0 = newest).
    function trendX(innerW, n, age, compressed) {
        const step0 = trendStep0(innerW, n, compressed);
        if (!compressed) {
            return innerW - age * step0;
        }
        return innerW - (step0 * (1 - Math.pow(TREND_DECAY, age))) / (1 - TREND_DECAY);
    }
    // Inverse of trendX: the sample age (float, samples) at inner-area x.
    function trendAgeOf(innerW, n, x, compressed) {
        const step0 = trendStep0(innerW, n, compressed);
        if (!compressed) {
            return (innerW - x) / step0;
        }
        const d = Math.max(0, Math.min(innerW, innerW - x));
        const ratio = 1 - (d * (1 - TREND_DECAY)) / step0;
        if (ratio <= 0) {
            return n - 1;
        }
        return Math.log(ratio) / Math.log(TREND_DECAY);
    }
    function seriesPaths(values, innerW, innerH, max, xOf) {
        const span = max > 0 ? max : 1;
        const points = values
            .map((v, i) => {
            const x = Math.round(xOf(i) * 100) / 100;
            const y = Math.round((innerH - (v / span) * innerH) * 100) / 100;
            return `${x},${y}`;
        })
            .join(" ");
        return { points, areaD: `M ${points} L ${innerW},${innerH} L 0,${innerH} Z` };
    }
    function arcPath(cx, cy, r, start, end) {
        const x1 = cx + r * Math.cos(start);
        const y1 = cy - r * Math.sin(start);
        const x2 = cx + r * Math.cos(end);
        const y2 = cy - r * Math.sin(end);
        const large = Math.abs(end - start) > Math.PI ? 1 : 0;
        return `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
    }
    function gaugeColor(frac) {
        if (frac < 0.6) {
            return "#57d977";
        }
        if (frac < 0.9) {
            return "#e6bd3a";
        }
        return "#ff7070";
    }
    function topBarClass(p95) {
        if (p95 > 16) {
            return "apm-bad";
        }
        if (p95 > 5) {
            return "apm-warn";
        }
        return "apm-ok";
    }
    function trendGrid(h, width, padLeft, padTop, innerH, max, yOf) {
        const fracs = [0, 0.25, 0.5, 0.75, 1];
        return h("g", null, fracs.map((f) => {
            const y = yOf(f * max);
            return h("g", { key: f }, h("line", { className: "apm-gridline", x1: padLeft, y1: y, x2: width, y2: y }), h("text", { className: "apm-axis-label", x: 4, y: y + 3, textAnchor: "start" }, `${Math.round(f * max)}`));
        }));
    }
    function trendSeriesSvg(h, s, innerW, innerH, max, yOf, xOf, hoverIdx) {
        const paths = seriesPaths(s.values, innerW, innerH, max, xOf);
        const gradId = `apg-${s.key}`;
        // Leading-edge current-value marker: rests on the newest sample and is the
        // eye anchor; the pointer drags it along the trace while hovering.
        const markerIdx = hoverIdx >= 0 ? hoverIdx : s.values.length - 1;
        return h("g", { key: s.key }, h("defs", null, h("linearGradient", { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, h("stop", { offset: "0%", stopColor: s.color, stopOpacity: 0.3 }), h("stop", { offset: "100%", stopColor: s.color, stopOpacity: 0.02 }))), h("polygon", { points: `0,${innerH} ${paths.points} ${innerW},${innerH}`, fill: `url(#${gradId})` }), h("polyline", { points: paths.points, fill: "none", stroke: s.color, strokeWidth: 1.5 }), h("circle", {
            cx: xOf(markerIdx), cy: yOf(s.values[markerIdx]), r: 3,
            fill: s.color, stroke: "rgba(0,0,0,.6)", strokeWidth: 1
        }));
    }
    // Faint vertical grid: one line per TREND_GRID_S of real time, drawn behind
    // the series. Uniform mode spaces them evenly; compressed mode bunches them
    // toward the left edge, which is what makes the timescale taper visible.
    function trendVGrid(h, innerW, n, padLeft, padTop, innerH, compressed) {
        const maxAgeS = (n - 1) * TREND_SAMPLE_S;
        const lines = [];
        for (let t = TREND_GRID_S; t < maxAgeS; t += TREND_GRID_S) {
            const x = padLeft + trendX(innerW, n, t / TREND_SAMPLE_S, compressed);
            lines.push(h("line", { key: t, className: "apm-vgrid", x1: x, y1: padTop, x2: x, y2: padTop + innerH }));
        }
        return h("g", null, lines);
    }
    function trendLegend(h, series, hoverIdx, secondsPerSample) {
        const idx = hoverIdx >= 0 ? hoverIdx : series[0].values.length - 1;
        const ago = Math.round((series[0].values.length - 1 - idx) * secondsPerSample);
        return h("div", { className: "apm-legend" }, series.map((s) => h("span", { key: s.key, className: "apm-legend-chip" }, h("span", { className: "apm-legend-swatch", style: { background: s.color }, "aria-hidden": true }), h("span", null, `${s.label} `), h("strong", { className: "apm-legend-value" }, s.format(s.values[idx])))), h("span", { className: "apm-axis-label" }, hoverIdx >= 0 ? `${ago}s ago` : "live"));
    }
    function renderTrendsChart(h, React, H, depth, onDepth) {
        const [hoverIdx, setHoverIdx] = React.useState(-1);
        const [compressed, setCompressed] = React.useState(true);
        const width = 600;
        const height = 150;
        const padLeft = 36;
        const padTop = 10;
        const padBottom = 20;
        const innerW = width - padLeft;
        const innerH = height - padTop - padBottom;
        const n = H.tps.length;
        // Empty structure: the frame, grids, and scale render before the first two
        // samples arrive; absence of signal stays visible instead of a text-only box.
        if (n < 2) {
            return h("div", { className: "apm-chart apm-trends" }, trendControls(h, depth, onDepth, compressed, setCompressed), h("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Line chart axes for TPS and gmUpdate ms; collecting samples." }, trendGrid(h, width, padLeft, padTop, innerH, niceMax(1), (v) => padTop + innerH - (v / niceMax(1)) * innerH), h("text", { className: "apm-axis-label", x: width / 2, y: height / 2, textAnchor: "middle" }, "collecting samples…")));
        }
        const series = trendSeriesOf(H);
        const max = niceMax(Math.max(...series.reduce((acc, s) => [...acc, ...s.values], []), 1));
        const xOf = (i) => trendX(innerW, n, n - 1 - i, compressed);
        const yOf = (v) => padTop + innerH - (v / max) * innerH;
        const crossX = hoverIdx >= 0 ? padLeft + xOf(hoverIdx) : -1;
        const onMove = (e) => {
            // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: SAFETY: the chart svg is the handler target
            const rect = e.currentTarget.getBoundingClientRect();
            const x = Math.max(0, Math.min(innerW, e.clientX - rect.left - padLeft));
            const age = trendAgeOf(innerW, n, x, compressed);
            setHoverIdx(Math.max(0, Math.min(n - 1, Math.round(n - 1 - age))));
        };
        return h("div", { className: "apm-chart apm-trends" }, trendControls(h, depth, onDepth, compressed, setCompressed), h("svg", {
            width, height, viewBox: `0 0 ${width} ${height}`, onMouseMove: onMove,
            onMouseLeave: () => setHoverIdx(-1),
            role: "img",
            // The hover crosshair is pointer-driven; keyboard and screen-reader
            // users get the series values from the text legend below the chart.
            "aria-label": `Line chart: TPS and gmUpdate ms over the last ${Math.round(n * TREND_SAMPLE_S)} seconds, timescale ${compressed ? "compressed (recent detail, older history tapers left)" : "uniform"}; latest values are listed in the legend below.`
        }, trendGrid(h, width, padLeft, padTop, innerH, max, yOf), trendVGrid(h, innerW, n, padLeft, padTop, innerH, compressed), h("g", null, series.map((s) => trendSeriesSvg(h, s, innerW, innerH, max, yOf, xOf, hoverIdx))), crossX >= 0 ? h("line", { className: "apm-crosshair", x1: crossX, y1: padTop, x2: crossX, y2: height - padBottom }) : null, h("text", { className: "apm-axis-label", x: padLeft, y: height - 4 }, `${Math.round(n * TREND_SAMPLE_S)}s ago`), h("text", { className: "apm-axis-label", x: width - 4, y: height - 4, textAnchor: "end" }, "now")), trendLegend(h, series, hoverIdx, TREND_SAMPLE_S));
    }
    // Chart head: history-depth setting plus the timescale toggle.
    function trendControls(h, depth, onDepth, compressed, setCompressed) {
        return h("div", { className: "apm-chart-head" }, h("button", { type: "button", className: "apm-btn", onClick: () => setCompressed(!compressed), "aria-pressed": compressed }, `Timescale: ${compressed ? "compressed" : "uniform"}`), h("select", {
            className: "apm-filter", "aria-label": "History depth",
            value: String(depth), onChange: (e) => {
                // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: SAFETY: the select is the handler target bound above
                onDepth(Number(e.target.value));
            }
        }, HISTORY_CHOICES.map((c) => h("option", { key: c, value: String(c) }, `${Math.round((c * TREND_SAMPLE_S) / 60)} min`))), h("span", { className: "apm-axis-label" }, "older history tapers left · grid lines are 30s apart"));
    }
    function renderBudgetGauge(h, update) {
        const width = 230;
        const height = 120;
        const cx = width / 2;
        const cy = height - 6;
        const r = 92;
        const avg = num(update.serverTickIntervalAvgMs);
        const frac = Math.min(1, avg / TICK_BUDGET_MS);
        return h("div", { className: "apm-chart apm-gauge" }, h("div", { className: "apm-gauge-title" }, "Tick vs budget"), h("svg", {
            width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
            "aria-label": `Average tick ${fx(avg, 1)} ms of the ${TICK_BUDGET_MS} ms budget (${Math.round(frac * 100)}% used).`
        }, h("path", { d: arcPath(cx, cy, r, Math.PI, 0), fill: "none", stroke: "#1d2631", strokeWidth: 14, strokeLinecap: "round" }), frac > 0
            ? h("path", { d: arcPath(cx, cy, r, Math.PI, Math.PI - frac * Math.PI), fill: "none", stroke: gaugeColor(frac), strokeWidth: 14, strokeLinecap: "round" })
            : null, h("text", { x: cx, y: cy - 30, textAnchor: "middle", className: "apm-gauge-value" }, `${fx(avg, 1)} ms`), h("text", { x: cx, y: cy - 12, textAnchor: "middle", className: "apm-gauge-label" }, `of ${TICK_BUDGET_MS} ms budget`)));
    }
    function renderTopSections(h, sections) {
        const top = [...sections].sort((a, b) => num(b.p95Ms) - num(a.p95Ms)).slice(0, 8);
        if (top.length === 0) {
            return null;
        }
        return h("div", { className: "apm-chart apm-topbars" }, h("h3", null, "Top sections by P95"), top.map((s) => {
            const p95 = num(s.p95Ms);
            // Fixed baseline: fraction of the 50 ms tick budget, not a per-render
            // maximum, so bar lengths stay comparable while the data streams.
            const frac = Math.min(1, p95 / TICK_BUDGET_MS);
            const note = severityNote(p95);
            return h("div", { key: s.name, className: "apm-topbar-row" }, h("span", { className: "apm-topbar-name" }, s.name), 
            // Decorative bar; the ms value beside it carries the data.
            h("div", { className: "apm-topbar-track", "aria-hidden": true }, h("div", { className: `apm-topbar-fill ${topBarClass(p95)}`, style: { width: `${Math.round(frac * 100)}%` } })), h("span", { className: "apm-topbar-val" }, `${fx(p95, 2)} ms`, note === null ? null : sr(h, note)));
        }));
    }
    function renderGrid(h, React, g, H, update, gc, world, health) {
        // oxlint-disable-next-line typescript/no-unnecessary-condition -- deliberate: the history arrays start empty; index access is undefined before the first sample
        const lastAlloc = H.alloc[H.alloc.length - 1];
        return h("div", { className: "apm-grid" }, trend(h, React, "TPS", H.tps, fx(g.tps, 1), "#57d977"), 
        // oxlint-disable-next-line typescript/no-unnecessary-condition -- deliberate: the history arrays start empty; index access is undefined at runtime before the first sample
        trend(h, React, "Gross alloc MiB/s", H.alloc, fx(lastAlloc !== null && lastAlloc !== void 0 ? lastAlloc : 0, 1), "#e6bd3a"), trend(h, React, "gmUpdate avg ms", H.gm, fx(update.gmUpdateDurationAvgMs, 2), "#8ab4f8"), cell(h, "Tick max", `${fx(update.serverTickIntervalMaxMs, 1)} ms`, null), cell(h, "gmUpdate max", `${fx(update.gmUpdateDurationMaxMs, 1)} ms`, null), cell(h, "Late ticks", `${num(update.lateTicks)} (${fx(update.tickStallMsTotal, 0)} ms)`, null), cell(h, "Spikes", num(update.totalSpikes), null), cell(h, "Players", `${num(world.players)} / ${num(world.clients)}`, null), cell(h, "Entities", `${num(world.entities)} (${num(world.entityAlives)} AI)`, null), cell(h, "GC gen0/s", fx(gc.gen0PerSecond, 1), null), cell(h, "GC gen2/s", fx(gc.gen2PerSecond, 2), rising(H.gen2) ? "apm-warn" : null), cell(h, "Heap", `${fx(mib(gc.heapBytes), 1)} MiB`, rising(H.heap) ? "apm-warn" : null), cell(h, "Working set", `${fx(mib(world.workingSetBytes), 1)} MiB`, null), cell(h, "Threads", num(world.threadCount), null), cell(h, "Dropped exports", num(health.droppedExports), num(health.droppedExports) > 0 ? "apm-warn" : null));
    }
    function bySortKey(sort) {
        return (a, b) => {
            // SAFETY: section rows come from the untyped JSON payload; sort.key is a known column of the same rows (bySortKey only keys on section columns)
            const av = sort.key === "name" ? a.name : num(a[sort.key]);
            // SAFETY: same keyed access as av, on the other row
            const bv = sort.key === "name" ? b.name : num(b[sort.key]);
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
    // Screen-reader-only text (see .apm-visually-hidden in styling.css). Used to
    // put non-visual words on state that the theme otherwise paints (severity
    // colors, staged-change ring).
    function sr(h, text) {
        return h("span", { className: "apm-visually-hidden" }, text);
    }
    // Same thresholds as sectionRowClass/topBarClass; gives the color-coded
    // severity a text equivalent so it does not ride on color alone.
    function severityNote(p95Ms) {
        const p95 = num(p95Ms);
        if (p95 > 16) {
            return " (well above tick budget)";
        }
        if (p95 > 5) {
            return " (above tick budget)";
        }
        return null;
    }
    function renderSectionsSection(h, React, sections, sort, setSortKey, filter, setFilter) {
        const shown = [...sections]
            .filter((s) => filter.length === 0 || strOrEmpty(s.name).toLowerCase().includes(filter.toLowerCase()))
            .sort(bySortKey(sort));
        const th = (label, key) => {
            let marker = "";
            let sortDir;
            if (sort.key === key) {
                marker = sort.dir < 0 ? " ▼" : " ▲";
                sortDir = sort.dir < 0 ? "descending" : "ascending";
            }
            // Sort affordance is a real button (keyboard operable); the column state
            // is exposed to AT via aria-sort on the header cell (WCAG 2.1.1 / 4.1.2).
            return h("th", { key: label, className: "apm-sortable", scope: "col", "aria-sort": sortDir }, h("button", { type: "button", className: "apm-sort-btn", onClick: () => setSortKey(key) }, `${label}${marker}`));
        };
        return [
            h("div", { className: "apm-sec-head" }, h("h3", null, "Managed sections"), h("input", {
                className: "apm-filter", type: "search", placeholder: "filter…",
                "aria-label": "Filter sections by name",
                value: filter, onChange: (e) => {
                    // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: SAFETY: the dashboard event target is the filter input the handler is bound to
                    setFilter(e.target.value);
                }
            })),
            h("table", { className: "apm-table" }, h("caption", { className: "apm-visually-hidden" }, "Managed sections timing"), h("thead", null, h("tr", null, th("Section", "name"), th("Calls", "calls"), th("Avg", "avgMs"), th("P95", "p95Ms"), th("P99", "p99Ms"), th("Max", "maxMs"), h("th", { key: "budget", scope: "col" }, "% of 50ms"))), h("tbody", null, shown.map((s) => {
                const frac = num(s.avgMs) / TICK_BUDGET_MS;
                const note = severityNote(num(s.p95Ms));
                return h("tr", { key: s.name, className: sectionRowClass(s) }, h("td", null, `${s.name}${s.deep === true ? " ·deep" : ""}`, note === null ? null : sr(h, note)), h("td", null, num(s.calls)), h("td", null, fx(s.avgMs, 3)), h("td", null, fx(s.p95Ms, 3)), h("td", null, fx(s.p99Ms, 3)), h("td", null, fx(s.maxMs, 3)), h("td", { className: "apm-budget-cell" }, budgetBar(React, frac, budgetBarClass(frac)), h("span", { className: "apm-budget-pct" }, `${fx(frac * 100, 1)}%`)));
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
            h("table", { className: "apm-table" }, h("caption", { className: "apm-visually-hidden" }, "Recent tick spikes"), h("thead", null, h("tr", null, headers.map((x) => h("th", { key: x, scope: "col" }, x)))), h("tbody", null, [...spikes].reverse().slice(0, 12).map((s, i) => h("tr", { key: i }, h("td", null, formatUtc(s.utc)), h("td", null, fx(s.gmUpdateDurationMs, 1)), h("td", null, fx(s.serverTickIntervalMs, 1)), h("td", null, num(objOrEmpty(s.world).players)), h("td", null, num(objOrEmpty(s.world).entities)))))),
        ];
    }
    function renderTransfersSection(h, transfers) {
        const headers = ["Package", "Count", "MiB", "Last bytes", "Max bytes"];
        return [
            h("h3", null, "Map and chunk transfers"),
            h("table", { className: "apm-table" }, h("caption", { className: "apm-visually-hidden" }, "Map and chunk transfers"), h("thead", null, h("tr", null, headers.map((x) => h("th", { key: x, scope: "col" }, x)))), h("tbody", null, transfers.map((t) => h("tr", { key: t.name }, h("td", null, t.name), h("td", null, num(t.packages)), h("td", null, fx(t.mebibytes, 2)), h("td", null, num(t.lastBytes)), h("td", null, num(t.maxBytes)))))),
        ];
    }
    function freezeHandler(opts) {
        if (!opts.frozen) {
            opts.frozenSnap.current = opts.live;
        }
        opts.setFrozen(!opts.frozen);
    }
    function togglePerfHandler(opts) {
        if (opts.perfBusy || !opts.perfAvailable) {
            return;
        }
        opts.setPerfBusy(true);
        // A no-op POST (config already in the requested state) answers 200 without
        // restarting, so busy must also clear on success or the button stays
        // disabled until a manual reload. The mod config can be missing (409
        // UNAVAILABLE) or unwritable (500 WRITE_FAILED); surface that instead of
        // silently snapping back to idle.
        void opts.HTTP.post("/api/perf", { enabled: !opts.perfEnabled })
            .then(() => {
            opts.setPerfError("");
            opts.setPerfBusy(false);
        })
            .catch(() => {
            opts.setPerfError("Perf toggle failed: the perf API rejected or dropped the request.");
            opts.setPerfBusy(false);
        });
    }
    function copySnapshot(snapshot, setCopyStatus) {
        const txt = JSON.stringify(snapshot, null, 2);
        // Clipboard requires a secure context; the dashboard may be served over
        // plain http. Either way, say what happened (role=status announces it).
        // oxlint-disable-next-line typescript/no-unnecessary-condition -- deliberate: clipboard is typed as always-present, but browsers omit it outside secure contexts
        if (navigator.clipboard === undefined) {
            setCopyStatus("Copy failed: clipboard is unavailable over plain HTTP.");
            return;
        }
        void navigator.clipboard.writeText(txt).then(() => setCopyStatus("Snapshot JSON copied to clipboard."), () => setCopyStatus("Copy failed: the clipboard write was rejected."));
    }
    // Feature-group row for the Efficiency panel. Toggles are staged locally
    // (pending), not applied per click; the Apply button commits them all with a
    // single restart. The row shows the effective (staged) state, a changed
    // marker, the description, and the safe/experimental status.
    // History-depth setting wired to a panel: the module variable is the single
    // source that pushHistory reads; changing it persists and trims old samples.
    function depthController(React, hist) {
        const [depth, setDepth] = React.useState(histDepth);
        const changeDepth = (n) => {
            histDepth = n;
            persistHistDepth(n);
            trimHistory(hist);
            setDepth(n);
        };
        return { depth, changeDepth };
    }
    function renderPerfGroupRow(h, g, on, staged, busy, toggle) {
        const status = strOr(g.status, "safe");
        const name = String(g.name);
        return h("div", { key: name, className: "apm-group" }, h("div", { className: "apm-group-info" }, h("span", { className: "apm-label" }, name), h("span", { className: "apm-group-desc" }, strOrEmpty(g.description))), h("div", { className: "apm-group-controls" }, h("span", { className: `apm-status ${status === "experimental" ? "apm-status-exp" : "apm-status-safe"}` }, status), h("span", { className: `apm-pill ${on ? "apm-ok" : "apm-off"}${staged ? " apm-staged" : ""}` }, on ? "ON" : "OFF", staged ? sr(h, " (change staged)") : null), 
        // The row repeats one visible label per group; include the group name so
        // the accessible name is unique and self-describing out of context.
        h("button", {
            type: "button", className: "apm-btn", disabled: busy, onClick: toggle,
            "aria-label": `${on ? "Turn off" : "Turn on"} ${name}`
        }, on ? "Turn off" : "Turn on")));
    }
    function ApmPanel({ React, HTTP, useQuery }) {
        var _a, _b;
        const h = React.createElement;
        // Authentication gate: an unauthenticated or non-admin session gets a 403
        // from /api/apm. Stop after the first failure instead of polling every 2 s
        // into an error storm (observed when a stale session cookie shows the entry
        // while logged out). retry:false skips react-query's default backoff retries.
        const [authBlocked, setAuthBlocked] = React.useState(false);
        const query = useQuery("seven-dtd-apm", () => HTTP.get("/api/apm"), { refetchInterval: 2000, enabled: !authBlocked, retry: false });
        React.useEffect(() => {
            var _a, _b;
            // Latch only on auth failures: a transient network drop or a coded 500
            // must not permanently freeze a live monitor on one bad poll.
            const status = (_b = (_a = query.error) === null || _a === void 0 ? void 0 : _a.response) === null || _b === void 0 ? void 0 : _b.status;
            if (query.isError === true && (status === 401 || status === 403)) {
                setAuthBlocked(true);
            }
        }, [query.isError, query.error]);
        const perfQ = useQuery("apm-perf", () => HTTP.get("/api/perf"), { refetchInterval: 30000, enabled: !authBlocked, retry: false });
        const [perfBusy, setPerfBusy] = React.useState(false);
        const [perfArmed, setPerfArmed] = React.useState(false);
        const hist = React.useRef({ last: null, tps: [], alloc: [], gm: [], gen2: [], heap: [] });
        const [frozen, setFrozen] = React.useState(false);
        const frozenSnap = React.useRef(null);
        const [filter, setFilter] = React.useState("");
        const [sort, setSort] = React.useState({ key: "p95Ms", dir: -1 });
        // Copy/freeze feedback for assistive tech (role=status announces changes).
        const [copyStatus, setCopyStatus] = React.useState("");
        // All hooks above; a failed fetch (e.g. logged-out session or logged-in
        // non-admin) renders a clear state instead of the NO DATA pills, and the
        // queries are paused (authBlocked) so nothing polls into an error storm.
        if (query.isError === true) {
            const status = (_b = (_a = query.error) === null || _a === void 0 ? void 0 : _a.response) === null || _b === void 0 ? void 0 : _b.status;
            return renderAuthError(h, "7DTD APM", status, "Authentication required: log in to the dashboard as an admin (permission level 0) to view server telemetry.", "Telemetry unavailable");
        }
        const live = unwrapSnap(query.data);
        const snapshot = frozen && frozenSnap.current !== null ? frozenSnap.current : live;
        if (!frozen && typeof live.utc === "string" && live.utc !== hist.current.last) {
            pushHistory(hist.current, live.utc, live);
        }
        const { update, health, gc, world, host, sections, transfers, spikes } = snapshotViewsOf(snapshot);
        const g = grade(update);
        const perf = unwrapSnap(perfQ.data);
        const perfEnabled = perf.enabled === true, perfAvailable = perf.available === true;
        const toggleFreeze = () => freezeHandler({ frozen, setFrozen, live, frozenSnap });
        // Restarting the server kicks every player for a couple of minutes, so the
        // toggle needs one explicit confirm click before it fires.
        const [perfError, setPerfError] = React.useState("");
        const togglePerf = () => armToggle(perfArmed, setPerfArmed, () => {
            togglePerfHandler({ HTTP, perfBusy, perfAvailable, setPerfBusy, perfEnabled, setPerfError });
        });
        const { depth, changeDepth } = depthController(React, hist.current);
        const setSortKey = (key) => setSort((s) => ({ key, dir: s.key === key ? -s.dir : -1 }));
        return h("div", { className: "seven-dtd-apm" }, renderHead(h, g, frozen, toggleFreeze, () => copySnapshot(snapshot, setCopyStatus), gc, update), h("span", { className: "apm-visually-hidden", role: "status" }, copyStatus), host === null ? null : renderHostStrip(h, host), renderPerfRow(h, perfEnabled, perfAvailable, perfBusy, perfArmed, togglePerf), perfError === "" ? null : h("pre", { className: "apm-error", role: "alert" }, perfError), renderTrendsChart(h, React, hist.current, depth, changeDepth), h("div", { className: "apm-charts-row" }, renderBudgetGauge(h, update), renderGrid(h, React, g, hist.current, update, gc, world, health)), renderTopSections(h, sections), strOrEmpty(health.lastExportError) === "" ? null : h("pre", { className: "apm-error", role: "alert" }, health.lastExportError), renderSectionsSection(h, React, sections, sort, setSortKey, filter, setFilter), renderSpikesSection(h, spikes), renderTransfersSection(h, transfers));
    }
    // Staged-apply helpers for the Efficiency panel: feature-group toggles are
    // staged locally (pending), not applied per click; the Apply button commits
    // them all with a single restart.
    function hasPending(pending, name) {
        return name in pending;
    }
    function effectiveOn(pending, g) {
        const name = String(g.name);
        return hasPending(pending, name) ? pending[name] : g.enabled === true;
    }
    function stageToggle(opts) {
        const name = String(opts.group.name);
        const current = opts.group.enabled === true;
        const next = !effectiveOn(opts.pending, opts.group);
        opts.setPerfError("");
        opts.setPending((p) => {
            const n = Object.assign({}, p);
            if (next === current) {
                delete n[name];
            }
            else {
                n[name] = next;
            }
            return n;
        });
    }
    function applyPerfGroups(opts) {
        if (opts.busy || opts.pendingCount === 0) {
            return;
        }
        opts.setArmedApply(false);
        opts.setBusy(true);
        void opts.HTTP.post("/api/perf", { groups: opts.pending })
            .then(() => {
            opts.setPending({});
            opts.setBusy(false);
        })
            .catch((error) => {
            opts.setPerfError(error instanceof Error ? error.message : String(error));
            opts.setBusy(false);
        });
    }
    // Feature-group list for the Efficiency panel: heading, staging hint, any
    // apply error, then one row per group.
    function renderFeatureGroups(h, groups, pending, busy, perfError, onStage) {
        return [
            h("h3", null, "Feature groups"),
            h("p", { className: "apm-window" }, `${groups.length} toggles · staged here, applied with one restart`),
            perfError === "" ? null : h("p", { className: "apm-error", role: "alert" }, perfError),
            h("div", { className: "apm-groups" }, groups.map((g) => renderPerfGroupRow(h, g, effectiveOn(pending, g), hasPending(pending, String(g.name)), busy, () => onStage(g)))),
        ];
    }
    // Focused panel for the EfficientServer perf mod toggle (its own top-level
    // menu entry alongside APM). Same /api/perf admin endpoint.
    function EfficiencyPanel({ React, HTTP, useQuery }) {
        var _a, _b;
        const h = React.createElement;
        const [blocked, setBlocked] = React.useState(false);
        const [busy, setBusy] = React.useState(false);
        const [pending, setPending] = React.useState({});
        const [armedApply, setArmedApply] = React.useState(false);
        const [perfError, setPerfError] = React.useState("");
        const perfQ = useQuery("apm-perf-efficiency", () => HTTP.get("/api/perf"), { refetchInterval: 30000, enabled: !blocked, retry: false });
        React.useEffect(() => {
            var _a, _b;
            // Auth-only latch, same rationale as ApmPanel: one dropped poll must not
            // kill a live panel.
            const status = (_b = (_a = perfQ.error) === null || _a === void 0 ? void 0 : _a.response) === null || _b === void 0 ? void 0 : _b.status;
            if (perfQ.isError === true && (status === 401 || status === 403)) {
                setBlocked(true);
            }
        }, [perfQ.isError, perfQ.error]);
        // All hooks above the conditional return: an error on a later refetch must
        // not change the hook count between renders (Rules of Hooks).
        const [toggleArmed, setToggleArmed] = React.useState(false);
        if (perfQ.isError === true) {
            const status = (_b = (_a = perfQ.error) === null || _a === void 0 ? void 0 : _a.response) === null || _b === void 0 ? void 0 : _b.status;
            return renderAuthError(h, "Efficiency", status, "Authentication required: log in to the dashboard as an admin (permission level 0) to control the perf mod.", "Perf API unavailable");
        }
        const perf = unwrapSnap(perfQ.data);
        const enabled = perf.enabled === true;
        const available = perf.available === true;
        const groups = listOrEmpty(perf.groups);
        // Same two-step confirm as the APM panel: flipping the whole mod restarts
        // the server, so a single stray click must not do it.
        const toggle = () => armToggle(toggleArmed, setToggleArmed, () => {
            togglePerfHandler({ HTTP, perfBusy: busy, perfAvailable: available, setPerfBusy: setBusy, perfEnabled: enabled, setPerfError });
        });
        const pendingCount = Object.keys(pending).length;
        const apply = () => applyPerfGroups({ HTTP, busy, pending, pendingCount, setArmedApply, setPending, setBusy, setPerfError });
        return h("div", { className: "seven-dtd-apm" }, h("div", { className: "apm-head" }, h("h2", null, "Efficiency"), h("span", { className: `apm-pill ${enabled ? "apm-ok" : "apm-warn"}` }, enabled ? "ENABLED" : "DISABLED")), h("div", { className: "apm-perf" }, h("span", { className: "apm-label" }, "Performance mod (EfficientServer)"), h("button", {
            type: "button", className: "apm-btn", disabled: busy || !available, onClick: toggle,
            "aria-label": toggleArmed && !busy
                ? `Confirm ${enabled ? "disable" : "enable"} now and restart the server`
                : undefined
        }, perfToggleLabel(busy, enabled, toggleArmed)), h("span", { className: "apm-window" }, available ? "flips the whole mod, restarts the server (~1-2 min)" : "perf config unavailable on this server")), ...renderFeatureGroups(h, groups, pending, busy, perfError, (g) => stageToggle({ pending, setPending, setPerfError, group: g })), h("div", { className: "apm-perf" }, h("button", {
            type: "button",
            className: `apm-btn apm-primary${armedApply ? " apm-armed" : ""}`,
            disabled: busy || pendingCount === 0,
            "aria-label": armedApply ? `Confirm apply of ${pendingCount} change${pendingCount === 1 ? "" : "s"} and restart now` : undefined,
            onClick: () => armToggle(armedApply, setArmedApply, apply)
        }, armedApply ? "Confirm apply?" : `Apply ${pendingCount} change${pendingCount === 1 ? "" : "s"} & restart`), pendingCount > 0
            ? h("button", { type: "button", className: "apm-btn", disabled: busy, onClick: () => setPending({}) }, "Discard")
            : null, h("span", { className: "apm-window" }, "stages changes · one restart applies them all")));
    }
    // The stock dashboard renders every webmod `routes` entry as a direct sidebar
    // item and every `settings` entry as a tab under Settings, unconditionally.
    // The session cookie is set HttpOnly (see ../7dtd-engine-research/docs), so it
    // is invisible to document.cookie and cannot gate registration here. Register
    // both panels always: while logged out they poll once, get a 403, and render
    // their auth-required state; the dashboard reloads the page after login.
    const webMod = {
        about: "Live, low-overhead managed telemetry from 7dtd-server-apm-bridge.",
        routes: { "APM": ApmPanel, "Efficiency": EfficiencyPanel },
        settings: {},
        mapComponents: []
    };
    Object.assign(globalThis, { [modId]: webMod });
    globalThis.dispatchEvent(new Event(`mod:${modId}:ready`));
})();
