"use strict";
(() => {
    const modId = "7dtd-server-apm-bridge";
    const HIST = 60;
    const TICK_BUDGET_MS = 50;
    const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
    const fx = (v, n) => num(v).toFixed(n);
    const mib = (bytes) => num(bytes) / 1048576;
    function objOrEmpty(candidate) {
        if (candidate === undefined || candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) {
            return {};
        }
        return candidate;
    }
    function listOrEmpty(candidate) {
        if (!Array.isArray(candidate)) {
            return [];
        }
        return candidate;
    }
    function strOrEmpty(candidate) {
        if (candidate === undefined || candidate === null) {
            return "";
        }
        return String(candidate);
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
        const refs = [0.25, 0.5, 0.75].map((f) => React.createElement("line", {
            key: f, x1: 0, y1: h * f, x2: w, y2: h * f,
            stroke: "rgba(127,127,127,.14)", strokeWidth: 1, vectorEffect: "non-scaling-stroke"
        }));
        return React.createElement("svg", { className: "apm-spark", width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none", "aria-hidden": true }, React.createElement("defs", null, React.createElement("linearGradient", { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, React.createElement("stop", { offset: "0%", stopColor: color, stopOpacity: 0.35 }), React.createElement("stop", { offset: "100%", stopColor: color, stopOpacity: 0.02 }))), ...refs, React.createElement("polygon", { points: `0,${h} ${pts} ${w},${h}`, fill: `url(#${gradId})` }), React.createElement("polyline", { points: pts, fill: "none", stroke: color, strokeWidth: 1.5, vectorEffect: "non-scaling-stroke" }), React.createElement("circle", { cx: lastX, cy: lastY, r: 2, fill: color }));
    }
    function budgetBar(React, frac, cls) {
        const pct = Math.max(0, Math.min(1, frac));
        return React.createElement("div", { className: "apm-bar", "aria-hidden": true }, React.createElement("div", { className: `apm-bar-fill ${cls}`, style: { transform: `scaleX(${pct.toFixed(4)})` } }));
    }
    function unwrapSnap(o) {
        if (typeof o !== "object" || o === null) {
            return {};
        }
        const record = o;
        const { data } = record;
        if (typeof data !== "object" || data === null) {
            return record;
        }
        const innerRecord = data;
        const inner = innerRecord.data;
        if (typeof inner === "object" && inner !== null) {
            return inner;
        }
        if (innerRecord.schema !== undefined || innerRecord.update !== undefined) {
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
        const authProblem = status === 401 || status === 403;
        const msg = authProblem
            ? authMessage
            : `${unavailablePrefix} (HTTP ${status !== null && status !== void 0 ? status : "error"}). Retrying every 2s; the panel fills in on its own once the bridge answers.`;
        const pill = authProblem ? "AUTH REQUIRED" : "UNAVAILABLE";
        return h("div", { className: "seven-dtd-apm" }, h("h2", null, title), h("span", { className: "apm-pill apm-bad" }, pill), h("p", null, msg), authProblem
            ? h("button", { type: "button", className: "apm-btn", onClick: () => { location.href = "/"; } }, "Log in")
            : null);
    }
    const STALE_AFTER_MS = 10000;
    function sampleStale(utc) {
        const sampled = Date.parse(utc);
        return Number.isFinite(sampled) && Date.now() - sampled > STALE_AFTER_MS;
    }
    function renderHead(h, g, frozen, toggleFreeze, copyJson, gc, update, utc) {
        return h("div", { className: "apm-head" }, h("h2", null, "7DTD APM"), h("span", { className: `apm-pill ${g.cls}` }, g.label), h("button", { type: "button", className: "apm-btn", onClick: toggleFreeze }, h("span", { "aria-hidden": true }, frozen ? "▶ " : "⏸ "), frozen ? "Resume" : "Freeze"), h("button", { type: "button", className: "apm-btn", onClick: copyJson }, h("span", { "aria-hidden": true }, "⧉ "), "Copy JSON"), h("span", { className: `apm-window${!frozen && sampleStale(utc) ? " apm-stale" : ""}` }, `window ${fx(gc.windowSeconds, 0)}s · ${num(update.windowUpdates)} ticks${update.deep === true ? " · deep" : ""}${utc === "" ? "" : ` · updated ${formatUtc(utc)} UTC`}${frozen ? " · FROZEN" : ""}`));
    }
    function trendSeriesOf(H) {
        return [
            { key: "tps", label: "TPS", values: H.tps, color: "rgb(var(--apm-ok-rgb))", format: (v) => v.toFixed(1) },
            { key: "gm", label: "gmUpdate ms", values: H.gm, color: "rgb(var(--apm-link-rgb))", format: (v) => v.toFixed(2) },
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
    const TREND_DECAY = 0.93;
    const TREND_GRID_S = 30;
    const TREND_SAMPLE_S = 2;
    const HISTORY_KEY = "apm.historySamples";
    const HISTORY_CHOICES = [60, 150, 300];
    let histDepth = HIST;
    try {
        const stored = Number(globalThis.localStorage.getItem(HISTORY_KEY));
        if (HISTORY_CHOICES.includes(stored)) {
            histDepth = stored;
        }
    }
    catch (_a) {
    }
    function persistHistDepth(samples) {
        try {
            globalThis.localStorage.setItem(HISTORY_KEY, String(samples));
        }
        catch (_a) {
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
    function trendStep0(innerW, n, compressed) {
        if (!compressed) {
            return innerW / (n - 1);
        }
        return (innerW * (1 - TREND_DECAY)) / (1 - Math.pow(TREND_DECAY, n));
    }
    function trendX(innerW, n, age, compressed) {
        const step0 = trendStep0(innerW, n, compressed);
        if (!compressed) {
            return innerW - age * step0;
        }
        return innerW - (step0 * (1 - Math.pow(TREND_DECAY, age))) / (1 - TREND_DECAY);
    }
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
            return "rgb(var(--apm-ok-rgb))";
        }
        if (frac < 0.9) {
            return "rgb(var(--apm-accent-rgb))";
        }
        return "rgb(var(--apm-bad-rgb))";
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
    function trendGrid(h, width, padLeft, max, yOf) {
        const fracs = [0, 0.25, 0.5, 0.75, 1];
        return h("g", null, fracs.map((f) => {
            const y = yOf(f * max);
            return h("g", { key: f }, h("line", { className: "apm-gridline", x1: padLeft, y1: y, x2: width, y2: y }), h("text", { className: "apm-axis-label", x: 4, y: y + 3, textAnchor: "start" }, `${Math.round(f * max)}`));
        }));
    }
    function trendSeriesSvg(h, s, innerW, innerH, max, yOf, xOf, hoverIdx) {
        const paths = seriesPaths(s.values, innerW, innerH, max, xOf);
        const gradId = `apg-${s.key}`;
        const markerIdx = hoverIdx >= 0 ? hoverIdx : s.values.length - 1;
        return h("g", { key: s.key }, h("defs", null, h("linearGradient", { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, h("stop", { offset: "0%", stopColor: s.color, stopOpacity: 0.3 }), h("stop", { offset: "100%", stopColor: s.color, stopOpacity: 0.02 }))), h("polygon", { points: `0,${innerH} ${paths.points} ${innerW},${innerH}`, fill: `url(#${gradId})` }), h("polyline", { points: paths.points, fill: "none", stroke: s.color, strokeWidth: 1.5 }), h("circle", {
            cx: xOf(markerIdx), cy: yOf(s.values[markerIdx]), r: 3,
            fill: s.color, stroke: "rgba(0,0,0,.6)", strokeWidth: 1
        }));
    }
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
        if (n < 2) {
            return h("div", { className: "apm-chart apm-trends" }, trendControls(h, depth, onDepth, compressed, setCompressed), h("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Line chart axes for TPS and gmUpdate ms; collecting samples." }, trendGrid(h, width, padLeft, niceMax(1), (v) => padTop + innerH - (v / niceMax(1)) * innerH), h("text", { className: "apm-axis-label", x: width / 2, y: height / 2, textAnchor: "middle" }, "collecting samples…")));
        }
        const series = trendSeriesOf(H);
        const max = niceMax(Math.max(...series.reduce((acc, s) => [...acc, ...s.values], []), 1));
        const xOf = (i) => trendX(innerW, n, n - 1 - i, compressed);
        const yOf = (v) => padTop + innerH - (v / max) * innerH;
        const crossX = hoverIdx >= 0 ? padLeft + xOf(hoverIdx) : -1;
        const setHoverAt = (svg, clientX) => {
            const rect = svg.getBoundingClientRect();
            const x = Math.max(0, Math.min(innerW, clientX - rect.left - padLeft));
            const age = trendAgeOf(innerW, n, x, compressed);
            setHoverIdx(Math.max(0, Math.min(n - 1, Math.round(n - 1 - age))));
        };
        const onMove = (e) => {
            setHoverAt(e.currentTarget, e.clientX);
        };
        const onTouch = (e) => {
            const touch = e.touches.item(0);
            if (touch === null) {
                return;
            }
            setHoverAt(e.currentTarget, touch.clientX);
        };
        return h("div", { className: "apm-chart apm-trends" }, trendControls(h, depth, onDepth, compressed, setCompressed), h("svg", {
            width, height, viewBox: `0 0 ${width} ${height}`, onMouseMove: onMove,
            onMouseLeave: () => setHoverIdx(-1),
            onTouchStart: onTouch, onTouchMove: onTouch,
            onTouchEnd: () => setHoverIdx(-1),
            role: "img",
            "aria-label": `Line chart: TPS and gmUpdate ms over the last ${Math.round(n * TREND_SAMPLE_S)} seconds, timescale ${compressed ? "compressed (recent detail, older history tapers left)" : "uniform"}; latest values are listed in the legend below.`
        }, trendGrid(h, width, padLeft, max, yOf), trendVGrid(h, innerW, n, padLeft, padTop, innerH, compressed), h("g", null, series.map((s) => trendSeriesSvg(h, s, innerW, innerH, max, yOf, xOf, hoverIdx))), crossX >= 0 ? h("line", { className: "apm-crosshair", x1: crossX, y1: padTop, x2: crossX, y2: height - padBottom }) : null, h("text", { className: "apm-axis-label", x: padLeft, y: height - 4 }, `${Math.round(n * TREND_SAMPLE_S)}s ago`), h("text", { className: "apm-axis-label", x: width - 4, y: height - 4, textAnchor: "end" }, "now")), trendLegend(h, series, hoverIdx, TREND_SAMPLE_S));
    }
    function trendControls(h, depth, onDepth, compressed, setCompressed) {
        return h("div", { className: "apm-chart-head" }, h("button", { type: "button", className: "apm-btn", onClick: () => setCompressed(!compressed), "aria-pressed": compressed }, `Timescale: ${compressed ? "compressed" : "uniform"}`), h("select", {
            className: "apm-filter", "aria-label": "History depth",
            value: String(depth), onChange: (e) => {
                onDepth(Number(e.target.value));
            }
        }, HISTORY_CHOICES.map((c) => h("option", { key: c, value: String(c) }, `${Math.round((c * TREND_SAMPLE_S) / 60)} min`))), h("span", { className: "apm-axis-label" }, compressed
            ? "older history tapers left · grid lines are 30s apart"
            : "equal width per sample · grid lines are 30s apart"));
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
        }, h("path", { className: "apm-gauge-track", d: arcPath(cx, cy, r, Math.PI, 0), fill: "none", strokeWidth: 14, strokeLinecap: "round" }), frac > 0
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
            const frac = Math.min(1, p95 / TICK_BUDGET_MS);
            const note = severityNote(p95);
            return h("div", { key: s.name, className: "apm-topbar-row" }, h("span", { className: "apm-topbar-name" }, s.name), h("div", { className: "apm-topbar-track", "aria-hidden": true }, h("div", { className: `apm-topbar-fill ${topBarClass(p95)}`, style: { transform: `scaleX(${frac.toFixed(4)})` } })), h("span", { className: "apm-topbar-val" }, `${fx(p95, 2)} ms`, note === null ? null : sr(h, note)));
        }));
    }
    function renderGrid(h, React, g, H, update, gc, world, health) {
        const lastAlloc = H.alloc[H.alloc.length - 1];
        return h("div", { className: "apm-grid" }, trend(h, React, "TPS", H.tps, fx(g.tps, 1), "rgb(var(--apm-ok-rgb))"), trend(h, React, "Gross alloc MiB/s", H.alloc, fx(lastAlloc !== null && lastAlloc !== void 0 ? lastAlloc : 0, 1), "rgb(var(--apm-accent-rgb))"), trend(h, React, "gmUpdate avg ms", H.gm, fx(update.gmUpdateDurationAvgMs, 2), "rgb(var(--apm-link-rgb))"), cell(h, "Tick max", `${fx(update.serverTickIntervalMaxMs, 1)} ms`, null), cell(h, "gmUpdate max", `${fx(update.gmUpdateDurationMaxMs, 1)} ms`, null), cell(h, "Late ticks", `${num(update.lateTicks)} (${fx(update.tickStallMsTotal, 0)} ms)`, null), cell(h, "Spikes", num(update.totalSpikes), null), cell(h, "Players", `${num(world.players)} / ${num(world.clients)}`, null), cell(h, "Entities", `${num(world.entities)} (${num(world.entityAlives)} AI)`, null), cell(h, "GC gen0/s", fx(gc.gen0PerSecond, 1), null), cell(h, "GC gen2/s", fx(gc.gen2PerSecond, 2), rising(H.gen2) ? "apm-warn" : null), cell(h, "Heap", `${fx(mib(gc.heapBytes), 1)} MiB`, rising(H.heap) ? "apm-warn" : null), cell(h, "Working set", `${fx(mib(world.workingSetBytes), 1)} MiB`, null), cell(h, "Threads", num(world.threadCount), null), cell(h, "Dropped exports", num(health.droppedExports), num(health.droppedExports) > 0 ? "apm-warn" : null), cell(h, "API errors", `${num(health.apiErrors)} / ${num(health.apiRequests)}`, num(health.apiErrors) > 0 ? "apm-warn" : null));
    }
    function healthAlerts(h, health) {
        const alerts = [];
        const sources = [
            ["export", strOrEmpty(health.lastExportError)],
            ["world sample", strOrEmpty(health.lastSampleError)],
            ["host", strOrEmpty(health.hostError)],
            ["api", strOrEmpty(health.lastApiError)]
        ];
        for (const [source, detail] of sources) {
            if (detail !== "") {
                alerts.push(h("pre", { key: source, className: "apm-error", role: "alert" }, `${source}: ${detail}`));
            }
        }
        return alerts;
    }
    function bySortKey(sort) {
        return (a, b) => {
            const av = sort.key === "name" ? a.name : num(a[sort.key]);
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
    function sr(h, text) {
        return h("span", { className: "apm-visually-hidden" }, text);
    }
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
            return h("th", { key: label, className: "apm-sortable", scope: "col", "aria-sort": sortDir }, h("button", { type: "button", className: "apm-sort-btn", onClick: () => setSortKey(key) }, `${label}${marker}`));
        };
        return [
            h("div", { className: "apm-sec-head" }, h("h3", null, "Managed sections"), h("input", {
                className: "apm-filter", type: "search", placeholder: "filter…",
                "aria-label": "Filter sections by name",
                value: filter, onChange: (e) => {
                    setFilter(e.target.value);
                }
            })),
            h("div", { className: "apm-table-scroll" }, h("table", { className: "apm-table" }, h("caption", { className: "apm-visually-hidden" }, "Managed sections timing"), h("thead", null, h("tr", null, th("Section", "name"), th("Calls", "calls"), th("Avg", "avgMs"), th("P95", "p95Ms"), th("P99", "p99Ms"), th("Max", "maxMs"), h("th", { key: "budget", scope: "col" }, "% of 50ms"))), h("tbody", null, shown.length === 0
                ? h("tr", null, h("td", { className: "apm-empty", colSpan: 7 }, sections.length === 0
                    ? "No section timings were collected for this window."
                    : `No section matches “${filter}”. Clear the filter to see all ${sections.length}.`))
                : shown.map((s) => {
                    const frac = num(s.avgMs) / TICK_BUDGET_MS;
                    const note = severityNote(num(s.p95Ms));
                    return h("tr", { key: s.name, className: sectionRowClass(s) }, h("td", null, `${s.name}${s.deep === true ? " ·deep" : ""}`, note === null ? null : sr(h, note)), h("td", null, num(s.calls)), h("td", null, fx(s.avgMs, 3)), h("td", null, fx(s.p95Ms, 3)), h("td", null, fx(s.p99Ms, 3)), h("td", null, fx(s.maxMs, 3)), h("td", { className: "apm-budget-cell" }, budgetBar(React, frac, budgetBarClass(frac)), h("span", { className: "apm-budget-pct" }, `${fx(frac * 100, 1)}%`)));
                })))),
        ];
    }
    const SPIKE_ROWS = 12;
    function renderSpikesSection(h, spikes) {
        const headers = ["When (UTC)", "gmUpdate ms", "Tick ms", "Players", "Entities"];
        return [
            h("h3", null, "Recent spikes"),
            h("div", { className: "apm-table-scroll" }, h("table", { className: "apm-table" }, h("caption", { className: "apm-visually-hidden" }, "Recent tick spikes"), h("thead", null, h("tr", null, headers.map((x) => h("th", { key: x, scope: "col" }, x)))), h("tbody", null, spikes.length === 0
                ? h("tr", null, h("td", { className: "apm-empty", colSpan: headers.length }, "No tick spikes recorded in this window."))
                : [...spikes].reverse().slice(0, SPIKE_ROWS).map((s, i) => h("tr", { key: i }, h("td", null, formatUtc(s.utc)), h("td", null, fx(s.gmUpdateDurationMs, 1)), h("td", null, fx(s.serverTickIntervalMs, 1)), h("td", null, num(objOrEmpty(s.world).players)), h("td", null, num(objOrEmpty(s.world).entities))))))),
        ];
    }
    function renderTransfersSection(h, transfers) {
        const headers = ["Package", "Count", "MiB", "Last bytes", "Max bytes"];
        return [
            h("h3", null, "Map and chunk transfers"),
            h("div", { className: "apm-table-scroll" }, h("table", { className: "apm-table" }, h("caption", { className: "apm-visually-hidden" }, "Map and chunk transfers"), h("thead", null, h("tr", null, headers.map((x) => h("th", { key: x, scope: "col" }, x)))), h("tbody", null, transfers.length === 0
                ? h("tr", null, h("td", { className: "apm-empty", colSpan: headers.length }, "No transfers recorded yet."))
                : transfers.map((t) => h("tr", { key: t.name }, h("td", null, t.name), h("td", null, num(t.packages)), h("td", null, fx(t.mebibytes, 2)), h("td", null, num(t.lastBytes)), h("td", null, num(t.maxBytes))))))),
        ];
    }
    function freezeHandler(opts) {
        if (!opts.frozen) {
            opts.frozenSnap.current = opts.live;
        }
        opts.setFrozen(!opts.frozen);
    }
    const COPY_STATUS_MS = 8000;
    let copyStatusTimer = null;
    function setCopyMessage(setCopyStatus, message) {
        setCopyStatus(message);
        if (copyStatusTimer !== null) {
            clearTimeout(copyStatusTimer);
        }
        copyStatusTimer = setTimeout(() => setCopyStatus(""), COPY_STATUS_MS);
    }
    function copySnapshot(snapshot, setCopyStatus) {
        const txt = JSON.stringify(snapshot, null, 2);
        if (navigator.clipboard === undefined) {
            setCopyMessage(setCopyStatus, "Copy failed: clipboard is unavailable over plain HTTP.");
            return;
        }
        void navigator.clipboard.writeText(txt).then(() => setCopyMessage(setCopyStatus, "Snapshot JSON copied to clipboard."), () => setCopyMessage(setCopyStatus, "Copy failed: the clipboard write was rejected."));
    }
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
    function ApmPanel({ React, HTTP, useQuery }) {
        var _a, _b;
        const h = React.createElement;
        const [authBlocked, setAuthBlocked] = React.useState(false);
        const query = useQuery("seven-dtd-apm", () => HTTP.get("/api/apm"), { refetchInterval: 2000, enabled: !authBlocked, retry: false });
        React.useEffect(() => {
            var _a, _b;
            const status = (_b = (_a = query.error) === null || _a === void 0 ? void 0 : _a.response) === null || _b === void 0 ? void 0 : _b.status;
            if (query.isError === true && (status === 401 || status === 403)) {
                setAuthBlocked(true);
            }
        }, [query.isError, query.error]);
        const hist = React.useRef({ last: null, tps: [], alloc: [], gm: [], gen2: [], heap: [] });
        const [frozen, setFrozen] = React.useState(false);
        const frozenSnap = React.useRef(null);
        const [filter, setFilter] = React.useState("");
        const [sort, setSort] = React.useState({ key: "p95Ms", dir: -1 });
        const [copyStatus, setCopyStatus] = React.useState("");
        const { depth, changeDepth } = depthController(React, hist.current);
        if (query.isError !== true && query.data === undefined) {
            return h("div", { className: "seven-dtd-apm" }, h("div", { className: "apm-head" }, h("h2", null, "7DTD APM")), h("p", { className: "apm-status" }, "Loading telemetry…"));
        }
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
        const toggleFreeze = () => freezeHandler({ frozen, setFrozen, live, frozenSnap });
        const setSortKey = (key) => setSort((s) => ({ key, dir: s.key === key ? -s.dir : -1 }));
        return h("div", { className: "seven-dtd-apm" }, renderHead(h, g, frozen, toggleFreeze, () => copySnapshot(snapshot, setCopyStatus), gc, update, strOrEmpty(snapshot.utc)), copyStatus === "" ? null : h("p", { className: "apm-status", role: "status" }, copyStatus), host === null ? null : renderHostStrip(h, host), renderTrendsChart(h, React, hist.current, depth, changeDepth), h("div", { className: "apm-charts-row" }, renderBudgetGauge(h, update), renderGrid(h, React, g, hist.current, update, gc, world, health)), renderTopSections(h, sections), ...healthAlerts(h, health), renderSectionsSection(h, React, sections, sort, setSortKey, filter, setFilter), renderSpikesSection(h, spikes), renderTransfersSection(h, transfers));
    }
    const webMod = {
        about: "Live, low-overhead managed telemetry from 7dtd-server-apm-bridge.",
        routes: { "APM": ApmPanel },
        settings: {},
        mapComponents: []
    };
    Object.assign(globalThis, { [modId]: webMod });
    globalThis.dispatchEvent(new Event(`mod:${modId}:ready`));
})();
