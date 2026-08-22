"use strict";
// 7dtd-apm-bridge WebMod (TypeScript source).
// Compiled to bundle.js by `tsc -p WebMod/tsconfig.json` (wired into
// scripts/build_bridge.sh). The dashboard loads /webmods/7dtd-apm-bridge/bundle.js
// and reads window["7dtd-apm-bridge"]: routes render as direct sidebar entries
// (hidden until the sid session cookie is present), settings as Settings tabs.
// Do not hand-edit bundle.js; regenerate from this file.
//
// The whole body is an IIFE on purpose: webmod bundles are plain <script> tags
// sharing the global scope, and a bare top-level const (e.g. modId) collides
// across mods (SyntaxError kills the later bundle's registration).
(() => {
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
        if (candidate === undefined || candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) {
            return {};
        }
        // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; the guard above proves the runtime value is a plain object
        return candidate;
    }
    function listOrEmpty(candidate) {
        if (!Array.isArray(candidate)) {
            return [];
        }
        // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; Array.isArray is the runtime proof for the element cast
        return candidate;
    }
    function strOrEmpty(candidate) {
        if (candidate === undefined || candidate === null) {
            return "";
        }
        // oxlint-disable-next-line typescript/no-base-to-string -- deliberate: payload values are JSON primitives (numbers, strings); String() renders them into labels
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
        return React.createElement("svg", { className: "apm-spark", width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none" }, React.createElement("defs", null, React.createElement("linearGradient", { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, React.createElement("stop", { offset: "0%", stopColor: color, stopOpacity: 0.35 }), React.createElement("stop", { offset: "100%", stopColor: color, stopOpacity: 0.02 }))), React.createElement("polygon", { points: `0,${h} ${pts} ${w},${h}`, fill: `url(#${gradId})` }), React.createElement("polyline", { points: pts, fill: "none", stroke: color, strokeWidth: 1.5 }), React.createElement("circle", { cx: lastX, cy: lastY, r: 2, fill: color }));
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
        // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; typeof above proves the runtime value is an object
        const record = o;
        const { data } = record;
        if (typeof data !== "object" || data === null) {
            return record;
        }
        // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; typeof above proves the runtime value is an object
        const innerRecord = data;
        const inner = innerRecord.data;
        if (typeof inner === "object" && inner !== null) {
            // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: untyped JSON payload boundary; typeof above proves the runtime value is an object
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
            if (arr.length > HIST) {
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
        return h("div", { className: `apm-cell${cls !== null && cls !== "" ? ` ${cls}` : ""}` }, h("span", { className: "apm-label" }, label), h("strong", null, valueText !== null && valueText !== void 0 ? valueText : "—"));
    }
    function trend(h, React, label, series, cur, color) {
        return h("div", { className: "apm-cell apm-trend" }, h("span", { className: "apm-label" }, label), h("strong", null, cur), spark(React, series, color, 130, 30));
    }
    function formatUtc(utc) {
        return strOrEmpty(utc).replace("T", " ").replace(/\..*$/u, "");
    }
    function renderAuthError(h, title, status, authMessage, unavailablePrefix) {
        const msg = status === 403 ? authMessage : `${unavailablePrefix} (HTTP ${status !== null && status !== void 0 ? status : "error"}).`;
        return h("div", { className: "seven-dtd-apm" }, h("h2", null, title), h("span", { className: "apm-pill apm-bad" }, "AUTH REQUIRED"), h("p", null, msg), h("button", { className: "apm-btn", onClick: () => { location.href = "/"; } }, "Log in"));
    }
    function renderHead(h, g, frozen, toggleFreeze, copyJson, gc, update) {
        return h("div", { className: "apm-head" }, h("h2", null, "7DTD APM"), h("span", { className: `apm-pill ${g.cls}` }, g.label), h("button", { className: "apm-btn", onClick: toggleFreeze }, frozen ? "▶ Resume" : "⏸ Freeze"), h("button", { className: "apm-btn", onClick: copyJson }, "⧉ Copy JSON"), h("span", { className: "apm-window" }, `window ${fx(gc.windowSeconds, 0)}s · ${num(update.windowUpdates)} ticks${update.deep === true ? " · deep" : ""}${frozen ? " · FROZEN" : ""}`));
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
    function seriesPaths(values, width, height, max) {
        const span = max > 0 ? max : 1;
        const n = values.length;
        const step = n > 1 ? width / (n - 1) : 0;
        const points = values
            .map((v, i) => {
            const x = Math.round(i * step * 100) / 100;
            const y = Math.round((height - (v / span) * height) * 100) / 100;
            return `${x},${y}`;
        })
            .join(" ");
        return { points, areaD: `M ${points} L ${width},${height} L 0,${height} Z` };
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
    function trendSeriesSvg(h, s, innerW, innerH, max, yOf, hoverIdx) {
        const paths = seriesPaths(s.values, innerW, innerH, max);
        const gradId = `apg-${s.key}`;
        const hoverCircle = hoverIdx >= 0
            ? h("circle", { cx: (hoverIdx * innerW) / (s.values.length - 1), cy: yOf(s.values[hoverIdx]), r: 3, fill: s.color })
            : null;
        return h("g", { key: s.key }, h("defs", null, h("linearGradient", { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, h("stop", { offset: "0%", stopColor: s.color, stopOpacity: 0.3 }), h("stop", { offset: "100%", stopColor: s.color, stopOpacity: 0.02 }))), h("polygon", { points: `0,${innerH} ${paths.points} ${innerW},${innerH}`, fill: `url(#${gradId})` }), h("polyline", { points: paths.points, fill: "none", stroke: s.color, strokeWidth: 1.5 }), hoverCircle);
    }
    function trendLegend(h, series, hoverIdx, secondsPerSample) {
        const idx = hoverIdx >= 0 ? hoverIdx : series[0].values.length - 1;
        const ago = Math.round((series[0].values.length - 1 - idx) * secondsPerSample);
        return h("div", { className: "apm-legend" }, series.map((s) => h("span", { key: s.key, className: "apm-legend-chip" }, h("span", { className: "apm-legend-swatch", style: { background: s.color } }), h("span", null, `${s.label} `), h("strong", { className: "apm-legend-value" }, s.format(s.values[idx])))), h("span", { className: "apm-axis-label" }, hoverIdx >= 0 ? `${ago}s ago` : "live"));
    }
    function renderTrendsChart(h, React, H) {
        const [hoverIdx, setHoverIdx] = React.useState(-1);
        const width = 600;
        const height = 150;
        const padLeft = 36;
        const padTop = 10;
        const padBottom = 20;
        const innerW = width - padLeft;
        const innerH = height - padTop - padBottom;
        const n = H.tps.length;
        if (n < 2) {
            return h("div", { className: "apm-chart apm-trends" }, "Collecting samples for the trends chart…");
        }
        const series = trendSeriesOf(H);
        const max = niceMax(Math.max(...series.reduce((acc, s) => [...acc, ...s.values], []), 1));
        const step = innerW / (n - 1);
        const yOf = (v) => padTop + innerH - (v / max) * innerH;
        const crossX = hoverIdx >= 0 ? padLeft + hoverIdx * step : -1;
        const onMove = (e) => {
            // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: the chart svg is the handler target
            const rect = e.currentTarget.getBoundingClientRect();
            const idx = Math.round((e.clientX - rect.left - padLeft) / step);
            setHoverIdx(Math.max(0, Math.min(n - 1, idx)));
        };
        return h("div", { className: "apm-chart apm-trends" }, h("svg", { width, height, viewBox: `0 0 ${width} ${height}`, onMouseMove: onMove, onMouseLeave: () => setHoverIdx(-1) }, trendGrid(h, width, padLeft, padTop, innerH, max, yOf), h("g", null, series.map((s) => trendSeriesSvg(h, s, innerW, innerH, max, yOf, hoverIdx))), crossX >= 0 ? h("line", { className: "apm-crosshair", x1: crossX, y1: padTop, x2: crossX, y2: height - padBottom }) : null, h("text", { className: "apm-axis-label", x: padLeft, y: height - 4 }, `${Math.round(n * 2)}s ago`), h("text", { className: "apm-axis-label", x: width - 4, y: height - 4, textAnchor: "end" }, "now")), trendLegend(h, series, hoverIdx, 2));
    }
    function renderBudgetGauge(h, update) {
        const width = 230;
        const height = 120;
        const cx = width / 2;
        const cy = height - 6;
        const r = 92;
        const avg = num(update.serverTickIntervalAvgMs);
        const frac = Math.min(1, avg / TICK_BUDGET_MS);
        return h("div", { className: "apm-chart apm-gauge" }, h("div", { className: "apm-gauge-title" }, "Tick vs budget"), h("svg", { width, height, viewBox: `0 0 ${width} ${height}` }, h("path", { d: arcPath(cx, cy, r, Math.PI, 0), fill: "none", stroke: "#1d2631", strokeWidth: 14, strokeLinecap: "round" }), frac > 0
            ? h("path", { d: arcPath(cx, cy, r, Math.PI, Math.PI - frac * Math.PI), fill: "none", stroke: gaugeColor(frac), strokeWidth: 14, strokeLinecap: "round" })
            : null, h("text", { x: cx, y: cy - 30, textAnchor: "middle", className: "apm-gauge-value" }, `${fx(avg, 1)} ms`), h("text", { x: cx, y: cy - 12, textAnchor: "middle", className: "apm-gauge-label" }, `of ${TICK_BUDGET_MS} ms budget`)));
    }
    function renderTopSections(h, sections) {
        const top = [...sections].sort((a, b) => num(b.p95Ms) - num(a.p95Ms)).slice(0, 8);
        if (top.length === 0) {
            return null;
        }
        const maxP95 = Math.max(...top.map((s) => num(s.p95Ms)), 1);
        return h("div", { className: "apm-chart apm-topbars" }, h("h3", null, "Top sections by P95"), top.map((s) => {
            const p95 = num(s.p95Ms);
            const frac = p95 / maxP95;
            return h("div", { key: s.name, className: "apm-topbar-row" }, h("span", { className: "apm-topbar-name" }, s.name), h("div", { className: "apm-topbar-track" }, h("div", { className: `apm-topbar-fill ${topBarClass(p95)}`, style: { width: `${Math.round(frac * 100)}%` } })), h("span", { className: "apm-topbar-val" }, `${fx(p95, 2)} ms`));
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
    function renderSectionsSection(h, React, sections, sort, setSortKey, filter, setFilter) {
        const shown = [...sections]
            .filter((s) => filter.length === 0 || strOrEmpty(s.name).toLowerCase().includes(filter.toLowerCase()))
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
                value: filter, onChange: (e) => {
                    // oxlint-disable-next-line typescript/no-unsafe-type-assertion -- deliberate: the dashboard event target is the filter input the handler is bound to
                    setFilter(e.target.value);
                }
            })),
            h("table", { className: "apm-table" }, h("thead", null, h("tr", null, th("Section", "name"), th("Calls", "calls"), th("Avg", "avgMs"), th("P95", "p95Ms"), th("P99", "p99Ms"), th("Max", "maxMs"), h("th", { key: "budget" }, "% of 50ms"))), h("tbody", null, shown.map((s) => {
                const frac = num(s.avgMs) / TICK_BUDGET_MS;
                return h("tr", { key: s.name, className: sectionRowClass(s) }, h("td", null, `${s.name}${s.deep === true ? " ·deep" : ""}`), h("td", null, num(s.calls)), h("td", null, fx(s.avgMs, 3)), h("td", null, fx(s.p95Ms, 3)), h("td", null, fx(s.p99Ms, 3)), h("td", null, fx(s.maxMs, 3)), h("td", { className: "apm-budget-cell" }, budgetBar(React, frac, budgetBarClass(frac)), h("span", { className: "apm-budget-pct" }, `${fx(frac * 100, 1)}%`)));
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
        void opts.HTTP.post("/api/perf", { enabled: !opts.perfEnabled }).catch(() => {
            opts.setPerfBusy(false);
        });
    }
    function copySnapshot(snapshot) {
        const txt = JSON.stringify(snapshot, null, 2);
        // oxlint-disable-next-line typescript/no-unnecessary-condition -- deliberate: clipboard requires a secure context; the dashboard may be served over plain http
        if (navigator.clipboard !== undefined) {
            void navigator.clipboard.writeText(txt);
        }
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
            if (query.isError === true) {
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
        if (query.isError === true) {
            const status = (_b = (_a = query.error) === null || _a === void 0 ? void 0 : _a.response) === null || _b === void 0 ? void 0 : _b.status;
            return renderAuthError(h, "7DTD APM", status, "Authentication required: log in to the dashboard as an admin (permission level 0) to view server telemetry.", "Telemetry unavailable");
        }
        const live = unwrapSnap(query.data);
        const snapshot = frozen && frozenSnap.current !== null ? frozenSnap.current : live;
        if (!frozen && typeof live.utc === "string" && live.utc !== hist.current.last) {
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
        const toggleFreeze = () => freezeHandler({ frozen, setFrozen, live, frozenSnap });
        const togglePerf = () => togglePerfHandler({ HTTP, perfBusy, perfAvailable, setPerfBusy, perfEnabled });
        const setSortKey = (key) => setSort((s) => ({ key, dir: s.key === key ? -s.dir : -1 }));
        return h("div", { className: "seven-dtd-apm" }, renderHead(h, g, frozen, toggleFreeze, () => copySnapshot(snapshot), gc, update), renderPerfRow(h, perfEnabled, perfAvailable, perfBusy, togglePerf), renderTrendsChart(h, React, hist.current), h("div", { className: "apm-charts-row" }, renderBudgetGauge(h, update), renderGrid(h, React, g, hist.current, update, gc, world, health)), renderTopSections(h, sections), strOrEmpty(health.lastExportError) === "" ? null : h("pre", { className: "apm-error" }, health.lastExportError), renderSectionsSection(h, React, sections, sort, setSortKey, filter, setFilter), renderSpikesSection(h, spikes), renderTransfersSection(h, transfers));
    }
    // Focused panel for the EfficientServer perf mod toggle (its own top-level
    // menu entry alongside APM). Same /api/perf admin endpoint.
    function EfficiencyPanel({ React, HTTP, useQuery }) {
        var _a, _b;
        const h = React.createElement;
        const [blocked, setBlocked] = React.useState(false);
        const perfQ = useQuery("apm-perf-efficiency", () => HTTP.get("/api/perf"), { refetchInterval: 30000, enabled: !blocked, retry: false });
        React.useEffect(() => {
            if (perfQ.isError === true) {
                setBlocked(true);
            }
        }, [perfQ.isError]);
        const [busy, setBusy] = React.useState(false);
        if (perfQ.isError === true) {
            const status = (_b = (_a = perfQ.error) === null || _a === void 0 ? void 0 : _a.response) === null || _b === void 0 ? void 0 : _b.status;
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
    Object.assign(globalThis, { [modId]: webMod });
    globalThis.dispatchEvent(new Event(`mod:${modId}:ready`));
})();
