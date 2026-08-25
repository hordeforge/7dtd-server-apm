"""Build the unified, schema-validated summary.json for a capture session.

Scores layers (cpu, memory/cache, sync/locks, scheduler, io, runtime/gc, app)
so you know where to optimize first. HTML rendering lives solely in
apm_suite.reporting; health lives solely in health.json.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

from ..io import atomic_json, iter_jsonl
from ..models import LayerScore, SummaryV2, as_number, layer_requested, schema_dict
from .bridge import attribute_document, attribute_snapshot


def parse_perf_stat(text: str) -> dict[str, float]:
    """Parse `perf stat -o` output into name->value."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "Performance counter" in line:
            continue
        elapsed = re.match(r"([\d,.]+)\s+seconds time elapsed", line)
        if elapsed:
            elapsed_s = as_number(elapsed.group(1).replace(",", ""))
            if elapsed_s is not None:
                out["time_elapsed_s"] = elapsed_s
            continue
        counter = re.match(r"([\d,.]+)\s+(\S+)", line)
        if not counter:
            continue
        raw, name = counter.group(1), counter.group(2).split(":")[0]
        # as_number, not bare float(): hw_stat.txt is re-read without schema
        # guarantees (imported bundles, hand edits), and a digit run past the
        # double range parses to inf - which then turns ipc/miss-rate into nan
        # (inf/inf) that persists into summary.json as a bare NaN strict JSON
        # consumers reject. A non-finite counter is absent evidence.
        number = as_number(raw.replace(",", ""))
        if number is not None:
            out[name] = number
    return out


def load_texts(session: Path) -> dict[str, str]:
    mapping = {
        "futex": session / "sync/futex.bt.out",
        "vfs": session / "io/vfs.bt.out",
        "block": session / "io/block.bt.out",
        "io_net": session / "io/io_net.bt.out",
        "runqlat": session / "scheduler/runqlat.bt.out",
        "states": session / "scheduler/states.bt.out",
        "offcpu": session / "scheduler/offcpu.bt.out",
        "oncpu": session / "cpu/oncpu.bt.out",
        "mono_gc": session / "runtime/mono_gc.bt.out",
        "hw": session / "memory/hw_stat.txt",
    }
    texts = {
        key: path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        for key, path in mapping.items()
    }
    # mono_alloc is deliberately absent: the (forensic-sized) probe output is
    # read once by _alloc_source_text in build_summary and shared with the
    # site rankings; loading it here as well held two full copies resident.
    app_path = session / "app/bridge.jsonl"
    parts: list[str] = []
    if app_path.exists():
        for record in iter_jsonl(app_path):
            parts.append(str(record.get("text") or record.get("error") or ""))
    texts["app"] = "\n".join(parts)
    return texts


def _cpu_layer(hw: dict[str, float], texts: dict[str, str]) -> LayerScore:
    cycles = hw.get("cycles") or hw.get("cpu-clock") or 0
    instructions = hw.get("instructions") or 0
    ipc = (instructions / cycles) if cycles else 0
    score = 0
    if ipc and ipc < 0.5:
        score += 40
    elif ipc and ipc < 1.0:
        score += 20
    if len(texts.get("oncpu", "")) > 500:
        score += 15
    return LayerScore(
        layer="cpu",
        score=min(100, score),
        signals={
            "ipc": round(ipc, 3) if ipc else None,
            "cycles": cycles,
            "instructions": instructions,
        },
        optimize=[
            "Reduce work on main sim thread (APM bridge managed sections)",
            "AI LOD / MaxSpawnedZombies / view distance",
            "Avoid Harmony on ultra-hot methods without budgets",
        ],
    )


def _cache_layer(hw: dict[str, float]) -> LayerScore:
    references = hw.get("cache-references") or 0
    misses = hw.get("cache-misses") or 0
    miss_rate = (misses / references) if references else 0
    score = 0
    if miss_rate > 0.15:
        score = 70
    elif miss_rate > 0.08:
        score = 40
    elif miss_rate > 0.03:
        score = 20
    llc_misses = hw.get("LLC-load-misses") or 0
    llc_loads = hw.get("LLC-loads") or 0
    if llc_loads and llc_misses / llc_loads > 0.2:
        score = max(score, 50)
    return LayerScore(
        layer="memory_cache",
        score=min(100, score),
        signals={
            "cache_miss_rate": round(miss_rate, 4) if references else None,
            "cache_misses": misses,
            "cache_references": references,
            "LLC_load_misses": llc_misses or None,
        },
        optimize=[
            "Reduce working set (chunks loaded, mesh queues)",
            "Avoid large per-tick allocations (GC pressure)",
            "Lower ServerMaxAllowedViewDistance / MaxQueuedMeshLayers",
        ],
    )


def _sync_layer(texts: dict[str, str], duration: float) -> LayerScore:
    futex_text = texts.get("futex", "")
    slow_futex = futex_text.count("SLOW_FUTEX")
    rate = slow_futex / duration
    main_wait = re.search(r"@main_futex_wait_us:\s*(\d+)", futex_text)
    main_wait_ms = int(main_wait.group(1)) / 1000 if main_wait else 0
    # SLOW_FUTEX is restricted to >=5 ms waits on the main process thread.
    # Worker futex durations are commonly intentional sleeps and are context,
    # not a contention score by themselves.
    if rate >= 2:
        score = 90
    elif rate >= 0.5:
        score = 65
    elif rate >= 0.1:
        score = 35
    elif slow_futex:
        score = 15
    else:
        score = 0
    # main-thread total futex wait time is a direct contention measure that
    # does not depend on the 5ms SLOW threshold.
    main_wait_share = main_wait_ms / (duration * 1000) if duration else 0
    if main_wait_share >= 0.1:
        score = max(score, 80)
    elif main_wait_share >= 0.03:
        score = max(score, 50)
    return LayerScore(
        layer="sync_locks",
        score=min(100, score),
        signals={
            "slow_futex_lines": slow_futex,
            "slow_futex_per_second": round(rate, 3),
            "main_thread_futex_wait_ms": round(main_wait_ms, 1),
            "main_thread_futex_wait_share": round(main_wait_share, 4),
            "threshold_ms": 5,
            "scope": "main_thread",
        },
        optimize=[
            "Inspect futex waiter stacks in sync/futex.bt.out",
            "Reduce lock hold time on main thread (Unity/Mono)",
            "Avoid sync IO under locks; move work off sim thread",
        ],
    )


def _scheduler_layer(texts: dict[str, str], duration: float) -> LayerScore:
    combined = texts.get("runqlat", "") + texts.get("states", "")
    offcpu = texts.get("offcpu", "")
    score = 0
    # bpftrace hist buckets use K/M/G suffixes ("[1K, 2K)"), so a bucket whose
    # LOWER bound carries a suffix is >= 1024 us ~ 1 ms. (The old raw-digit regex
    # never matched real output.)
    if re.search(r"\[\s*[0-9]+[KMG],", combined):  # off-CPU/runq block >= ~1ms
        score += 30
    if "preempt" in combined or "@preempted" in combined:
        score += 20
    # Main-thread off-CPU total is dominated by the healthy 20-TPS frame-pacing
    # sleep (server sleeps between ticks when under budget), which is NOT lag.
    # With fp unwinding that sleep fragments across many shallow stacks, so it
    # cannot be split by stack. D-state (uninterruptible, disk) blocks are the
    # one unambiguous pathological signal here; the "laggy" headline lives on
    # app_sim late ticks (bridge-measured tick overage), not on this total.
    stall_match = re.search(r"@stall_us_total:\s*(\d+)", offcpu)
    stall_ms = int(stall_match.group(1)) / 1000 if stall_match else 0
    d_state = re.search(r"@stall_state_us\[2\]:\s*(\d+)", offcpu)
    d_state_ms = int(d_state.group(1)) / 1000 if d_state else 0
    d_share = d_state_ms / (duration * 1000) if duration else 0
    stall_events = offcpu.count("STALL_MAIN")
    states = texts.get("states", "")
    runq = re.search(r"@main_runq_stall_us:\s*(\d+)", states)
    runq_stall_ms = int(runq.group(1)) / 1000 if runq else 0
    runq_events = states.count("MAIN_RUNQ_STALL")
    if d_share >= 0.05:
        score = max(score, 80)
    elif d_share >= 0.01:
        score = max(score, 50)
    elif d_state_ms > 0:
        score = max(score, 25)
    # Main tick thread ready but not scheduled = OS starvation (contention /
    # affinity), independent of the game's own CPU.
    if runq_stall_ms >= (duration * 1000 * 0.05):
        score = max(score, 75)
    elif runq_stall_ms > 20:
        score = max(score, 45)
    return LayerScore(
        layer="scheduler",
        score=min(100, score),
        signals={
            "main_thread_offcpu_ms": round(stall_ms, 1),
            "disk_block_ms": round(d_state_ms, 1),
            "disk_block_share": round(d_share, 4),
            "main_runq_stall_ms": round(runq_stall_ms, 1),
            "main_runq_stall_events": runq_events,
            "blocks_over_10ms": stall_events,
            "note": "off-CPU total includes healthy 20-TPS pacing sleep; see app_sim late_ticks",
        },
        optimize=[
            "app_sim.late_ticks is the lag headline (bridge tick overage)",
            "scheduler/offcpu.bt.out stall stacks attribute blocks to call sites",
            "D-state disk blocks on the tick thread = move IO off the main thread",
        ],
    )


def _io_layer(texts: dict[str, str]) -> LayerScore:
    vfs = texts.get("vfs", "")
    slow_block = texts.get("block", "").count("SLOW_BLOCK")
    main_io = vfs.count("SLOW_VFS_MAIN")
    score = min(
        100, slow_block * 15 + (20 if "slow_read_stack" in vfs or "@slow_read" in vfs else 0)
    )
    if main_io:
        score = max(score, min(100, 40 + main_io * 5))  # main-thread file IO stalls frames
    if "fsync" in vfs.lower():
        score = max(score, 10)
    return LayerScore(
        layer="io",
        score=min(100, score),
        signals={"slow_block_lines": slow_block, "main_thread_slow_io": main_io},
        optimize=[
            "NVMe for Saves; avoid network filesystem for region files",
            "Tune autosave interval; reduce map rendering",
            "Inspect vfs slow stacks + openat paths",
        ],
    )


def _gc_layer(texts: dict[str, str]) -> LayerScore:
    gc_text = texts.get("mono_gc", "")
    # The probe emits one slow token, "SLOW mono_gc_collect". "SLOW mono_gc" is a
    # prefix of it, so counting both double-counts every slow-collect line.
    slow_gc = gc_text.count("SLOW mono_gc_collect")
    # @little_n is printed each interval as a growing cumulative; take the LAST.
    little_hits = re.findall(r"@little_n:\s*(\d+)", gc_text)
    little = int(little_hits[-1]) if little_hits else 0
    # Direct stop-the-world freeze: total us all threads (incl. main tick) were
    # suspended, and worst single pause. This IS the "laggy without CPU" time.
    stw_sum = re.search(r"@stw_sum:\s*(\d+)", gc_text)
    stw_ms = int(stw_sum.group(1)) / 1000 if stw_sum else 0.0
    stw_pauses = gc_text.count("STW_PAUSE")
    worst_stw_ms = max(
        (int(m) / 1000 for m in re.findall(r"STW_PAUSE (\d+) us", gc_text)), default=0.0
    )
    score = min(100, slow_gc * 25 + min(40, little // 50))
    if worst_stw_ms >= 100:  # a >=100ms freeze is 2+ missed 50ms ticks
        score = max(score, 85)
    elif worst_stw_ms >= 50:
        score = max(score, 60)
    return LayerScore(
        layer="runtime_gc",
        score=score,
        signals={
            "slow_gc_lines": slow_gc,
            "collect_a_little_hits": little or None,
            "stw_pause_total_ms": round(stw_ms, 1),
            "stw_pause_count": stw_pauses,
            "stw_pause_worst_ms": round(worst_stw_ms, 1),
        },
        optimize=[
            "Cut allocations in hot Harmony paths",
            "Object pools for per-tick lists",
            "stw_pause_worst_ms is the direct main-thread freeze; cut gross alloc to shrink it",
        ],
    )


def _app_layer(texts: dict[str, str]) -> LayerScore:
    app = texts.get("app", "")
    score = 0
    if "TickEntities" in app or "tick" in app.lower():
        score += 10
    if "spike" in app.lower():
        score += 30
    return LayerScore(
        layer="app_sim",
        score=min(100, score),
        signals={"has_managed_bridge": bool(app.strip())},
        optimize=[
            "APM bridge snapshot / deep for C# sections",
            "sibling 7dtd-loadgen scenario to reproduce AI pressure",
            "EfficientServer AI LOD + mesh budgets",
        ],
    )


def layer_scores(session: Path, hw: dict[str, float], texts: dict[str, str]) -> list[LayerScore]:
    """Heuristic 0-100 severity scores (higher = more pressure), coverage-aware."""
    meta = _load_meta(session)
    duration = max(1.0, float(meta.get("seconds") or 1))
    scores = [
        _cpu_layer(hw, texts),
        _cache_layer(hw),
        _sync_layer(texts, duration),
        _scheduler_layer(texts, duration),
        _io_layer(texts),
        _gc_layer(texts),
        _app_layer(texts),
    ]
    requested = {
        token.strip() for token in str(meta.get("only") or "all").split(",") if token.strip()
    }
    sources = {
        "cpu": [session / "cpu/oncpu.bt.out", session / "cpu/perf/stacks.folded"],
        "memory_cache": [session / "memory/hw_stat.txt", session / "memory/proc.jsonl"],
        "sync_locks": [session / "sync/futex.bt.out"],
        "scheduler": [session / "scheduler/runqlat.bt.out", session / "scheduler/states.bt.out"],
        "io": [session / "io/vfs.bt.out", session / "io/block.bt.out"],
        "runtime_gc": [session / "runtime/mono_gc.bt.out"],
        "app_sim": [session / "app/apm_app.json", session / "app/bridge.jsonl"],
    }
    for score in scores:
        wanted = layer_requested(score.layer, requested)
        present = any(p.is_file() and p.stat().st_size for p in sources[score.layer])
        score.state = "collected" if present else "unavailable" if wanted else "skipped"
        score.confidence = "medium" if score.state == "collected" else "low"
        if score.state != "collected":
            score.score = None
            score.signals = {
                "reason": "collector produced no usable evidence"
                if wanted
                else "layer not requested"
            }
            score.optimize = []
    scores.sort(key=lambda s: -(s.score if s.score is not None else -1))
    return scores


def _as_tid(value: Any) -> int:
    """Normalize a jsonl row's tid to int for the main-thread match.

    threads.jsonl is re-read without schema guarantees (torn collector output,
    imported bundles): the collector writes int tids, but a float or string tid
    that fails the == match silently reports the main thread at 0% instead of
    raising - a wrong measurement is worse than a missing one.
    """
    number = as_number(value)
    return int(number) if number is not None else -1


def _num0(value: Any) -> float:
    """diagnose_lag's coercion for externally sourced scalars (bridge snapshot
    extra=allow fields, layer signals from re-read summaries, prior
    csharp_bridge.json shares): junk degrades to 0 instead of raising out of
    the required summary stage."""
    return as_number(value) or 0.0


def _int0(value: Any) -> int:
    return int(_num0(value))


def thread_summary(session: Path) -> dict[str, Any]:
    path = session / "threads/threads.jsonl"
    if not path.exists():
        return {}
    main_tid = 0
    with contextlib.suppress(ValueError, OSError):
        main_tid = int(_load_meta(session).get("pid") or 0)
    # Streaming fold: only the last intact record plus two running means are
    # needed, so retaining every parsed sample (each with its nested top list)
    # would hold the whole file as Python objects for nothing.
    last: dict[str, Any] | None = None
    main_cpu_sum = 0.0
    main_share_sum = 0.0
    averaged = 0

    def fold(sample: dict[str, Any]) -> None:
        # Main-thread saturation: averaged across samples, the main thread's
        # own CPU% and its share of total process CPU. High main share with a low
        # box-wide load = "laggy without CPU" that is really main-thread-bound.
        nonlocal main_cpu_sum, main_share_sum, averaged
        rows = sample.get("top") or []
        if not isinstance(rows, list):
            return
        # Junk-but-valid JSON values coerce to "no contribution" so a single
        # corrupt row cannot crash the required summary stage.
        # Denominator: the record's whole-process CPU% (the collector sums every
        # sampled thread into process_cpu_pct). The persisted top list is capped
        # (--top), so summing only those rows would overstate the main thread's
        # share of process CPU on many-thread servers whenever busier threads
        # than the cap exist. Legacy records without the field fall back to the
        # top-row sum (a documented approximation for sessions predating it).
        process_total = as_number(sample.get("process_cpu_pct"))
        total = (
            process_total
            if process_total is not None
            else sum(as_number(r.get("cpu_pct")) or 0.0 for r in rows if isinstance(r, dict))
        )
        main = next(
            (
                as_number(r.get("cpu_pct")) or 0.0
                for r in rows
                if isinstance(r, dict) and _as_tid(r.get("tid")) == main_tid
            ),
            0.0,
        )
        if total > 0:
            main_cpu_sum += main
            main_share_sum += main / total
            averaged += 1

    for record in iter_jsonl(path):
        last = record
        fold(record)
    if last is None:
        return {}
    main_cpu_avg = round(main_cpu_sum / averaged, 1) if averaged else None
    main_share_avg = round(main_share_sum / averaged, 3) if averaged else None
    return {
        "n_threads": last.get("n_threads"),
        "states": last.get("states"),
        "wchan_top": last.get("wchan_top"),
        "main_thread_cpu_pct_avg": main_cpu_avg,
        "main_thread_share_of_process_avg": main_share_avg,
        "top_cpu": [
            {
                "comm": t.get("comm"),
                "cpu_pct": t.get("cpu_pct"),
                "wchan": t.get("wchan"),
                "state": t.get("state"),
            }
            for t in (last.get("top") or [])[:8]
        ],
    }


def memory_trend(session: Path) -> dict[str, Any]:
    """RSS slope over the window: distinguishes GC churn (flat RSS, oscillating
    heap) from a real leak / unbounded buffer (RSS climbing). fd growth catches
    socket/handle leaks."""
    path = session / "memory/proc.jsonl"
    if not path.is_file():
        return {}
    times: list[float] = []
    monos: list[float | None] = []
    rss: list[float] = []
    fds: list[int] = []
    for record in iter_jsonl(path):
        # proc.jsonl is re-read without schema guarantees (torn collector
        # output, imported bundles): a non-numeric t/rss_mb must drop that one
        # record instead of raising out of the required summary stage.
        stamp = as_number(record.get("t"))
        rss_value = as_number(record.get("rss_mb"))
        if stamp is not None and rss_value is not None:
            times.append(stamp)
            # Samplers that also stamp a monotonic clock let the span below
            # ignore wall-clock steps; legacy records fall back to `t`.
            monos.append(as_number(record.get("mono")))
            rss.append(rss_value)
            raw_fd = record.get("fd_count")
            if isinstance(raw_fd, (int, float)) and not isinstance(raw_fd, bool) and raw_fd >= 0:
                fds.append(int(raw_fd))
    if len(rss) < 3:
        return {}
    # Elapsed-time denominator: prefer the sampler's monotonic stamps (a
    # wall-clock step mid-window would skew the MB/s verdict); legacy sessions
    # without them fall back to the wall stamps.
    if monos[0] is not None and monos[-1] is not None:
        span = monos[-1] - monos[0]
    else:
        span = times[-1] - times[0]
    rss_slope = (rss[-1] - rss[0]) / span if span > 0 else 0  # MB/s
    # fd_count is -1 (or absent) when its sample could not list /proc/pid/fd
    # (a /proc race), so it is UNKNOWN, not a real count: feeding the sentinel
    # into end-start arithmetic manufactured fd growth (+151 from a single
    # raced first sample) and fired false leak causes. `fds` holds only
    # measured counts (filtered during parsing); omit it entirely when none.
    result: dict[str, Any] = {
        "rss_start_mb": round(rss[0], 1),
        "rss_end_mb": round(rss[-1], 1),
        "rss_growth_mb_per_s": round(rss_slope, 3),
    }
    if fds:
        result["fd_start"] = fds[0]
        result["fd_end"] = fds[-1]
    return result


# Frames that are runtime/BCL/engine noise, never the real owner of a sample or
# allocation. 7DTD game types live in the global namespace (GameManager,
# AstarVoxelGrid) or resolve as Class.Method, so skipping these surfaces the game
# frame. UnityEngine.* is skipped: engine struct/util methods (e.g.
# Quaternion.FromToRotation) appear as leaf frames but do not own the cost - the
# real owner is the game caller below. Kept consistent between the alloc and CPU
# filters.
_NOISE_PREFIX = ("System.", "UnityEngine.", "Unity.", "Mono.", "Cysharp.", "Newtonsoft.")

# One bpftrace ustack map record: @name[\n <frames> \n]: <value>.
_ALLOC_RECORD = re.compile(r"@\w+\[\n(?P<frames>.*?)\n\s*\]:\s*(?P<value>\d+)", re.DOTALL)


def _alloc_stack_site(frames: list[str]) -> str | None:
    """First real game frame under the GC_malloc leaf, or None if unattributable.

    The leaf is always GC_malloc; just below it sit BCL/runtime frames (String.*,
    the profiler, unresolved hex). Walk down to the first frame that is actual
    game/engine code - that is the method responsible for the allocation.
    """
    for frame in frames:
        name = frame.split("+", 1)[0].strip()
        if not name or name.startswith("0x"):
            continue
        # GC_malloc-style leaves carry no Class.Method separator and are dropped
        # by the resolved-symbol check below along with native C symbols.
        if name.startswith(_NOISE_PREFIX):
            continue
        if "." not in name and "::" not in name:
            continue  # not a resolved Class.Method symbol
        return name
    return None


def _alloc_source_text(session: Path) -> str:
    """Full text of the mono_alloc probe output (jitsym-annotated when present)."""
    annotated = session / "runtime" / "mono_alloc.bt.annotated.txt"
    source = annotated if annotated.is_file() else session / "runtime" / "mono_alloc.bt.out"
    return source.read_text(encoding="utf-8", errors="replace") if source.is_file() else ""


def _alloc_block_sites(
    session: Path, header: str, limit: int, text: str | None = None
) -> list[str]:
    """Rank allocation sites in a bpftrace ustack block by total bytes.

    bpftrace prints maps ascending, so the biggest allocations are LAST; naive
    top-down reading grabs the smallest, noisiest stacks. Instead parse every
    (stack, bytes) record, attribute each to its owning game frame, aggregate by
    bytes, and return the true heaviest sites. Pass `text` (from
    _alloc_source_text) when ranking several blocks so the file is read once.
    """
    if text is None:
        text = _alloc_source_text(session)
        if not text:
            return []
    # Slice to just this section: from the header to the next "===" divider.
    block = text.partition(header)[2].split("\n===", 1)[0]
    totals: dict[str, int] = {}
    for match in _ALLOC_RECORD.finditer(block):
        frames = [ln.strip() for ln in match.group("frames").splitlines() if ln.strip()]
        site = _alloc_stack_site(frames)
        if site is None:
            continue
        totals[site] = totals.get(site, 0) + int(match.group("value"))
    return sorted(totals, key=lambda s: totals[s], reverse=True)[:limit]


# CPU-flame frame noise: native libs, kernel, unresolved, and the BCL/runtime.
# A "game frame" is Class.Method that is not one of the noise prefixes above.


def _is_game_frame(frame: str) -> bool:
    f = frame.strip()
    if not f or f.startswith("[") or f.startswith("0x"):
        return False  # [libc.so.6] / [unknown] / [jit] / raw hex
    if "." not in f and "::" not in f:
        return False  # native C symbol (GC_dirty_inner, __pthread_*)
    return not f.startswith(_NOISE_PREFIX)  # skip BCL / engine runtime


def _rank_folded(folded: Path, limit: int) -> dict[str, list[tuple[str, float]]]:
    if not folded.is_file():
        return {"inclusive": [], "self_game": []}
    inclusive: dict[str, int] = {}
    self_game: dict[str, int] = {}
    total = 0
    # Streamed line by line: folded stacks reach hundreds of MB and this runs
    # for both the aggregate and the main-thread view in one finalize.
    with folded.open("r", encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            # rstrip only the newline (never strip()): the count token must keep
            # its exact tail so `.isdigit()` judges malformed lines identically
            # to the former splitlines-based read.
            line = raw.rstrip("\n")
            sp = line.rsplit(" ", 1)
            if len(sp) != 2 or not sp[1].isdigit():
                continue
            frames = sp[0].split(";")
            try:
                count = int(sp[1])
            except ValueError:
                # isdigit() also accepts Digit-class characters ("²") that
                # int() rejects; a folded line carrying one (imported bundles)
                # must skip that row, not fail the required summary stage.
                continue
            total += count
            for fr in set(frames):  # inclusive: count a function once per stack
                inclusive[fr] = inclusive.get(fr, 0) + count
            # self_game: walk from the leaf down to the first game frame
            for fr in reversed(frames):
                if _is_game_frame(fr):
                    name = fr.split("+", 1)[0].strip()
                    self_game[name] = self_game.get(name, 0) + count
                    break
    if total == 0:
        return {"inclusive": [], "self_game": []}

    def rank(d: dict[str, int]) -> list[tuple[str, float]]:
        # Tiebreak on name: inclusive counts are built from set(frames)
        # iteration (hash-randomized per process), so equal-count entries
        # would otherwise reorder the top-N between renders of the same data.
        top = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [(k.split("+", 1)[0], round(100.0 * v / total, 1)) for k, v in top]

    return {"inclusive": rank(inclusive), "self_game": rank(self_game)}


def top_cpu_hot_paths(session: Path, limit: int = 12) -> dict[str, list[tuple[str, float]]]:
    """Rank hot paths from the symbolized perf folded stacks - comprehensive
    auto-discovery beyond the curated bridge sections. Views (name, percent):
      inclusive   - functions by total samples anywhere in the stack (all-thread,
        native kept); broad "where does aggregate CPU go".
      self_game   - leaf attributed to the first GAME frame (all-thread); which
        game code is hot across all cores (serialization, chunk, light...).
      main_thread - self_game restricted to the sim thread (tid==pid, from
        stacks.main.folded): the hot game code FOR THE 20 TPS TICK ITSELF - the
        thing that actually gates ms_per_tick, invisible in the all-thread views
        (the single sim thread is a small slice of aggregate CPU).
    """
    perf = session / "cpu/perf"
    agg = _rank_folded(perf / "stacks.folded", limit)
    main = _rank_folded(perf / "stacks.main.folded", limit)
    return {
        "inclusive": agg["inclusive"],
        "self_game": agg["self_game"],
        "main_thread": main["self_game"],
    }


def top_alloc_sites(session: Path, limit: int = 3, text: str | None = None) -> list[str]:
    """Top methods behind the biggest LARGE (>=4KB) allocations (heap spikes)."""
    return _alloc_block_sites(session, "top large-allocation sites by bytes", limit, text)


def top_churn_sites(session: Path, limit: int = 3, text: str | None = None) -> list[str]:
    """Top methods behind the steady small-object churn (1/4096 sampled, all sizes).

    This is the GC-pause FLOOR source, which the >=4KB large-alloc view misses.
    """
    return _alloc_block_sites(session, "top sampled (1/4096, all sizes)", limit, text)


def top_stack_sites(session: Path, rel: str, header: str, limit: int = 3) -> list[str]:
    """Leaf managed frame names from the top stacks of an annotated bt.out block.

    Generic version of top_alloc_sites: used to name lock-contention waiters and
    other blocking sites once jitsym has annotated the probe output.
    """
    stem = session / rel
    annotated = stem.with_suffix(".annotated.txt")
    source = annotated if annotated.is_file() else stem
    if not source.is_file():
        return []
    block = source.read_text(encoding="utf-8", errors="replace").partition(header)[2]
    sites: list[str] = []
    for line in block.splitlines():
        frame = line.strip()
        if frame and not frame.startswith("0x") and frame not in ("]", "@"):
            name = frame.split("+", 1)[0]
            if "." in name and name not in sites and not name.startswith("@"):
                sites.append(name)
        if len(sites) >= limit:
            break
    return sites


def diagnose_lag(
    layers: list[LayerScore],
    metadata: dict[str, Any],
    threads: dict[str, Any],
    attribution: dict[str, Any] | None = None,
    session: Path | None = None,
) -> dict[str, Any]:
    """Synthesize the layer signals into a ranked, plain-language lag verdict.

    "Laggy without CPU" has a few distinct root causes; this names which ones
    fired this session and how bad, so the reader gets an answer, not a table.
    """
    signals = {layer.layer: layer.signals for layer in layers if layer.state == "collected"}
    frame = metadata.get("frame") or {}
    gc = metadata.get("gc") or {}
    alloc_sites: list[str] = metadata.get("top_alloc_sites") or []
    churn_sites: list[str] = metadata.get("top_churn_sites") or []
    causes: list[dict[str, Any]] = []

    late = _int0(frame.get("lateTicks"))
    tick_stall = _num0(frame.get("tickStallMsTotal"))
    laggy = late > 0

    # Saturation / death-spiral: tick interval far over the 50 ms (20 TPS)
    # budget with essentially every tick late means the server is not merely
    # laggy, it is past its capacity (e.g. the ~0.3 TPS collapse at ~500
    # players). Surface it as the dominant cause so it is not mistaken for a
    # transient spike.
    tick_interval = _num0(frame.get("tickIntervalAvgMs"))
    window_updates = _int0(frame.get("windowUpdates"))
    tps = 1000 / tick_interval if tick_interval > 0 else 0
    late_share = late / window_updates if window_updates else 0
    saturated = tick_interval >= 150 and late_share >= 0.9
    if saturated:
        causes.append(
            {
                "cause": "server_saturated",
                "severity": 1.0,
                "detail": f"{tps:.2f} TPS ({tick_interval:.0f} ms/tick vs 50 ms budget), "
                f"{late}/{window_updates} ticks late - past capacity, not a transient spike",
                "fix": "reduce load or fix the dominant subsystem below; at player scale "
                "this is the connection/serialization wall (optimizer 4d)",
            }
        )

    # allocMBPerSecond is NET heap growth (GetTotalMemory delta); at steady
    # state alloc==collect so it reads ~0 even under heavy churn. The full-GC
    # count is the direct pause signal, and grossAllocMBPerSecond (bridge
    # GetTotalAllocatedBytes, monotonic) is the true pressure when available.
    net_heap = _num0(gc.get("allocMBPerSecond"))
    gross = _num0(gc.get("grossAllocMBPerSecond"))
    full_gc = _int0(gc.get("fullCollections"))
    window_s = _num0(gc.get("windowSeconds"))
    gc_rate = full_gc / window_s if window_s > 0 else 0  # full GCs per second
    gc_signals = signals.get("runtime_gc") or {}
    worst_stw = _num0(gc_signals.get("stw_pause_worst_ms"))
    stw_total = _num0(gc_signals.get("stw_pause_total_ms"))
    little = _int0(gc_signals.get("collect_a_little_hits"))
    little_rate = little / window_s if window_s > 0 else 0
    if gross >= 1.5 or full_gc >= 1 or worst_stw >= 50:
        pressure = f"{gross} MB/s gross alloc" if gross > 0 else f"{net_heap} MB/s net heap growth"
        # Two distinct drains from the same churn: rare big STW freezes vs. the
        # constant incremental collect_a_little nibbling the main thread.
        if worst_stw >= 50:
            stw_detail = f", worst freeze {worst_stw} ms ({stw_total} ms total STW)"
        elif little_rate >= 50:
            stw_detail = (
                f", incremental GC {little_rate:.0f}/s (small STW {stw_total} ms total; "
                "cost is spread across ticks, not one freeze)"
            )
        elif worst_stw > 0:
            stw_detail = f", worst freeze {worst_stw} ms ({stw_total} ms total)"
        else:
            stw_detail = ""
        causes.append(
            {
                "cause": "gc_pauses",
                "severity": round(min(1.0, gross / 8 + full_gc / 6 + worst_stw / 200), 2),
                "detail": f"{full_gc} full stop-the-world GC(s) ({gc_rate:.2f}/s), "
                f"{pressure}{stw_detail}",
                "fix": (
                    "cut per-tick allocations"
                    + (f"; large-alloc spikes: {', '.join(alloc_sites)}" if alloc_sites else "")
                    + (f"; steady churn: {', '.join(churn_sites)}" if churn_sites else "")
                    + "; guard dedicated GC.Collect (optimizer A7)"
                ),
            }
        )

    main_share = _num0(threads.get("main_thread_share_of_process_avg"))
    main_cpu = _num0(threads.get("main_thread_cpu_pct_avg"))
    if main_share >= 0.5:
        # Name the hot game code ON the tick (main-thread perf), not aggregate CPU.
        hot = (metadata.get("cpu_hot_paths") or {}).get("main_thread") or []
        hot_str = ", ".join(f"{n} {p}%" for n, p in hot[:6])
        causes.append(
            {
                "cause": "main_thread_bound",
                "severity": round(min(1.0, main_share), 2),
                "detail": f"main thread = {main_cpu}% CPU, {round(main_share * 100)}% of process "
                f"across {threads.get('n_threads')} threads"
                + (f"; tick hot paths: {hot_str}" if hot_str else ""),
                "fix": "do less on the tick (LOD/tier-skip entities) or extract sim off "
                "main (SIM_PARALLELISM); more cores do not help a single-thread bottleneck",
            }
        )

    disk_ms = _num0((signals.get("scheduler") or {}).get("disk_block_ms"))
    if disk_ms >= 50:
        causes.append(
            {
                "cause": "disk_io_stall",
                "severity": round(min(1.0, disk_ms / 5000), 2),
                "detail": f"{disk_ms} ms main-thread disk blocking",
                "fix": "move saves/region IO off the tick thread; NVMe for Saves; "
                "spread autosave (see io/vfs.bt.out SLOW_VFS_MAIN sites)",
            }
        )

    mem = metadata.get("memory") or {}
    rss_slope = _num0(mem.get("rss_growth_mb_per_s"))
    fd_growth = _int0(mem.get("fd_end")) - _int0(mem.get("fd_start"))
    if rss_slope >= 5 or fd_growth >= 100:
        causes.append(
            {
                "cause": "memory_growth",
                "severity": round(min(1.0, rss_slope / 20 + fd_growth / 500), 2),
                "detail": f"RSS +{rss_slope} MB/s, fd {fd_growth:+d} over the window",
                "fix": "unbounded buffer or leak (not GC churn: RSS climbs steadily); "
                "check send queues / caches / event subscriptions",
            }
        )

    runq_stall = _num0((signals.get("scheduler") or {}).get("main_runq_stall_ms"))
    if runq_stall >= 50:
        causes.append(
            {
                "cause": "cpu_starvation",
                "severity": round(min(1.0, runq_stall / 2000), 2),
                "detail": f"{runq_stall} ms main tick thread ready but not scheduled (OS starvation)",
                "fix": "pin/isolate server cores (taskset/cgroup), remove noisy neighbors "
                "(HOST_TUNING.md); not a game-code issue",
            }
        )

    sync_signals = signals.get("sync_locks") or {}
    slow_futex = _int0(sync_signals.get("slow_futex_lines"))
    wait_ms = _num0(sync_signals.get("main_thread_futex_wait_ms"))
    wait_share = _num0(sync_signals.get("main_thread_futex_wait_share"))
    # Fire on either the count of slow waits OR the main thread spending a real
    # slice of wall-clock blocked on locks (the direct "laggy without CPU" tell).
    if slow_futex >= 5 or wait_share >= 0.05:
        lock_sites = (
            top_stack_sites(session, "sync/futex.bt.out", "waiter stacks by count")
            if session
            else []
        )
        causes.append(
            {
                "cause": "lock_contention",
                "severity": round(min(1.0, max(slow_futex / 100, wait_share)), 2),
                "detail": f"{slow_futex} slow futex waits on the main thread"
                + (f"; {wait_ms} ms ({wait_share:.1%}) main-thread lock wait" if wait_ms else ""),
                "fix": "reduce main-thread lock hold time"
                + (
                    f" at {', '.join(lock_sites)}"
                    if lock_sites
                    else "; inspect sync/futex.bt.out waiter stacks"
                ),
            }
        )

    transfers = metadata.get("transfers") or {}
    bridge_mb_s = _num0(transfers.get("mb_per_second"))  # since-reset average
    kernel_send = _num0((metadata.get("net") or {}).get("udp_send_mb_per_second"))
    # Kernel UDP send is always the capture window; the bridge counter averages
    # since the last reset and is inflated by the initial join chunk burst.
    # Prefer the windowed kernel rate as the current headline when we have it.
    current_mb_s = kernel_send if kernel_send > 0 else bridge_mb_s
    if current_mb_s >= 20 or bridge_mb_s >= 20:
        burst_note = (
            f" (bridge since-reset avg {bridge_mb_s} MB/s is join-burst weighted)"
            if kernel_send > 0 and bridge_mb_s >= current_mb_s * 2
            else ""
        )
        causes.append(
            {
                "cause": "chunk_bandwidth",
                "severity": round(min(1.0, current_mb_s / 100), 2),
                "detail": f"{current_mb_s} MB/s chunk streaming "
                f"({transfers.get('packages_per_second')} pkg/s){burst_note}",
                "fix": "reduce chunk churn: lower view/sim distance, cluster players; "
                "moving players reload chunks constantly (B4)",
            }
        )

    subsystems = (attribution or {}).get("subsystems") or []
    # subsystem shares are of instrumented managed time; a dominant additive
    # bucket names the workload driving the frame even when no host probe fired.
    # frame_core (GameManager.UpdateTick) is INCLUSIVE of the others, so skip it
    # and name the top DISJOINT subsystem - else a network-bound tick where
    # frame_core is nominally top would surface nothing (missed the player wall).
    disjoint = [s for s in subsystems if str(s.get("subsystem")) not in ("frame_core",)]
    # Pick by share, not list order, so a mis-sorted attribution can't name the
    # wrong bottleneck.
    top = max(disjoint, key=lambda s: _num0(s.get("share")), default=None)
    if top:
        share = _num0(top.get("share"))
        name = str(top.get("subsystem"))
        friendly = {
            "network": "network serialization + entity distribution to clients",
            "io_saves": "chunk/region save + streaming",
            "entity_tick": "entity tick machinery",
            "mesh": "dynamic mesh",
        }.get(name, name)
        if share >= 0.45:
            causes.append(
                {
                    "cause": f"{name}_bound",
                    "severity": round(min(1.0, share), 2),
                    "detail": f"{friendly} = {round(share * 100)}% of instrumented managed time"
                    + (
                        " (per-player serialization/distribution is the player-scale wall)"
                        if name == "network"
                        else ""
                    ),
                    "fix": {
                        "network": "off-thread package serialization; spatially cull "
                        "NetEntityDistribution (per-player lists ~O(players x entities)); "
                        "batch ConnectionManager work (optimizer 4d/B3)",
                        "io_saves": "chunk view/sim distance + save cadence (B4); NVMe Saves",
                        "entity_tick": "tick-stride far entities at TickEntity level (A1/A3)",
                        "mesh": "tighter DynamicMesh budgets / OnlyPlayerAreas (B5)",
                    }.get(name, "see optimizer OPTIMIZATION_CANDIDATES.md"),
                }
            )

    causes.sort(key=lambda c: -float(c["severity"]))
    # Tick headroom: gmUpdate compute vs the 50ms (20 TPS) budget. Low compute
    # with late ticks = SPIKE-driven lag (GC pauses / stalls punctuating an
    # otherwise-paced server), the classic "laggy without CPU". High compute =
    # sustained saturation. This tells the reader which regime they are in.
    gm_avg = _num0(frame.get("gmUpdateAvgMs"))
    budget_ms = 50.0
    headroom_pct = round(max(0.0, (budget_ms - gm_avg) / budget_ms) * 100, 1) if gm_avg else None
    if headroom_pct is not None and laggy:
        if gm_avg < budget_ms * 0.6:
            profile = (
                f"spike-driven: avg compute {gm_avg:.1f} ms leaves {headroom_pct}% headroom, "
                "so lag is bursty (GC pauses / stalls), not sustained CPU"
            )
        else:
            profile = (
                f"compute-bound: avg compute {gm_avg:.1f} ms of the 50 ms budget "
                f"({headroom_pct}% headroom) - sustained work, not just spikes"
            )
    else:
        profile = None
    if not laggy:
        verdict = "server met its tick deadline this window"
    elif saturated:
        verdict = f"SATURATED ({tps:.2f} TPS, {late}/{window_updates} ticks late) - " + "; ".join(
            str(c["cause"]) for c in causes
        )
    elif causes:
        verdict = f"laggy ({late} late ticks, {round(tick_stall)} ms overage) - " + "; ".join(
            str(c["cause"]) for c in causes
        )
    else:
        verdict = f"laggy ({late} late ticks) but no instrumented cause dominated - check flames"
    result: dict[str, Any] = {"laggy": laggy, "verdict": verdict, "causes": causes}
    if saturated:
        result["saturated"] = True
        result["tps"] = round(tps, 2)
    if profile is not None:
        result["profile"] = profile
        result["tick_headroom_pct"] = headroom_pct
    return result


def _load_meta(session: Path) -> dict[str, Any]:
    meta_path = session / "meta.json"
    return json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}


def _apply_main_thread_pressure(layers: list[LayerScore], threads: dict[str, Any]) -> None:
    """One thread carrying most of the process while the box looks idle is
    main-thread-bound; near a full core is that ceiling."""
    share = threads.get("main_thread_share_of_process_avg")
    cpu = threads.get("main_thread_cpu_pct_avg")
    if share is None:
        return
    for layer in layers:
        if layer.layer != "cpu" or layer.state != "collected":
            continue
        layer.signals["main_thread_cpu_pct"] = cpu
        layer.signals["main_thread_share_of_process"] = share
        if share >= 0.5 and (cpu or 0) >= 80:
            layer.score = max(layer.score or 0, 85)
        elif share >= 0.5:
            layer.score = max(layer.score or 0, 55)


def _snapshot_metadata(snapshot: dict[str, Any], mono_alloc: str) -> dict[str, Any]:
    """world/frame/transfers/gc blocks derived from a bridge snapshot.

    Raises on malformed values so the caller decides what to keep; absent
    blocks are simply omitted from the result.
    """
    world = snapshot.get("world") or {}
    update = snapshot.get("update") or {}
    metadata: dict[str, Any] = {
        "world": {
            "entities": world.get("entities"),
            "players": world.get("players"),
            "entityAlives": world.get("entityAlives"),
            "clients": world.get("clients"),
        }
    }
    # engineGap = frame period minus managed gmUpdate: the unattributed
    # ENGINE slice (animator eval, transforms, player-loop overhead) that no
    # managed section can see. It is where headless-waste findings like the
    # zombie-animator path live; a healthy idle server shows ~frame-target
    # minus ~1-2 ms.
    frame_now = world.get("unityDeltaMs") or 0
    gm_avg = update.get("gmUpdateDurationAvgMs") or 0
    metadata["frame"] = {
        "gmUpdateAvgMs": update.get("gmUpdateDurationAvgMs"),
        "tickIntervalAvgMs": update.get("serverTickIntervalAvgMs"),
        "unityDeltaMs": world.get("unityDeltaMs"),
        "engineGapMs": round(frame_now - gm_avg, 2) if frame_now else None,
        "windowUpdates": update.get("windowUpdates"),
        "spikes": update.get("totalSpikes"),
        "lateTicks": update.get("lateTicks"),
        "tickStallMsTotal": update.get("tickStallMsTotal"),
    }
    # as_number (not bare float()): JSON "1e999" parses to inf, which passes
    # every truthiness/>0 check below, divides rates into misleading zeros,
    # and would persist into summary.json as a bare `Infinity` that strict
    # JSON consumers reject. Non-finite evidence is absent evidence.
    window_s = as_number((snapshot.get("gc") or {}).get("windowSeconds")) or 0.0
    transfers = snapshot.get("mapTransfers") or []
    if transfers and window_s > 0:
        total_bytes = sum(int(x.get("bytes") or 0) for x in transfers)
        total_pkgs = sum(int(x.get("packages") or 0) for x in transfers)
        metadata["transfers"] = {
            "mb_per_second": round(total_bytes / 1048576 / window_s, 2),
            "packages_per_second": round(total_pkgs / window_s, 1),
            "by_type": {
                str(x.get("name")): round(int(x.get("bytes") or 0) / 1048576, 1) for x in transfers
            },
        }
    gc_window = snapshot.get("gc") or {}
    if gc_window:
        heap_delta = int(as_number(gc_window.get("heapDeltaBytes")) or 0)
        # Boehm (Mono's default here) is non-generational: gen0==gen2, so
        # the collection counts are not a generational signal. The real
        # allocation-pressure gauge is heap growth rate; each full
        # collection it triggers is a stop-the-world frame hitch.
        alloc_mb_s = (heap_delta / 1048576 / window_s) if window_s > 0 else 0
        collections = int(as_number(gc_window.get("gen2Collections")) or 0)
        # Gross allocation is the real GC-pause driver. Unity 2022 Mono
        # lacks GC.GetTotalAllocatedBytes (bridge counter is -1), so the
        # opt-in mono_alloc probe (Boehm GC_malloc arg0) is the source
        # on this runtime. Left None when unmeasured so the budget gate
        # treats it as UNKNOWN, never a healthy zero.
        gross_bps = as_number(gc_window.get("grossAllocBytesPerSecond"))
        if gross_bps is None:
            # Missing or junk field: fall back to the bridge's own "unmeasured"
            # sentinel so the mono_alloc probe path below still runs.
            gross_bps = -1.0
        gross_mb_s: float | None = round(gross_bps / 1048576, 2) if gross_bps >= 0 else None
        if gross_mb_s is None and window_s > 0:
            alloc_match = re.search(r"@alloc_bytes_total:\s*(\d+)", mono_alloc)
            if alloc_match:
                gross_mb_s = round(int(alloc_match.group(1)) / 1048576 / window_s, 2)
        gc_meta: dict[str, Any] = {
            "allocMBPerSecond": round(alloc_mb_s, 2),  # net heap growth
            "fullCollections": collections,
            "heapDeltaBytes": heap_delta,
            "windowSeconds": round(window_s, 1),
        }
        if gross_mb_s is not None:
            gc_meta["grossAllocMBPerSecond"] = gross_mb_s  # true churn
            # Allocation per tick (KB): the garbage each tick creates that
            # Boehm must eventually scan. Ties churn to the tick budget.
            ticks = int(update.get("windowUpdates") or 0)
            if ticks and window_s > 0:
                alloc_kb_tick = gross_mb_s * 1024 * window_s / ticks
                gc_meta["grossAllocKBPerTick"] = round(alloc_kb_tick, 1)
        metadata["gc"] = gc_meta
    return metadata


def _apply_gc_pressure(layers: list[LayerScore], gc_meta: dict[str, Any]) -> None:
    """Raise runtime_gc on real allocation pressure. Scored on GROSS churn (the
    GC-pause driver) when measured; net heap growth reads ~0 at steady state and
    would leave the layer (and health) blind to real allocation pressure."""
    alloc_mb_s = float(gc_meta.get("allocMBPerSecond") or 0)
    gross = gc_meta.get("grossAllocMBPerSecond")
    churn = float(gross) if gross is not None else alloc_mb_s
    collections = int(gc_meta.get("fullCollections") or 0)
    for layer in layers:
        if layer.layer != "runtime_gc" or layer.state != "collected":
            continue
        layer.signals["alloc_mb_per_second"] = round(alloc_mb_s, 2)
        layer.signals["full_gc_collections"] = collections
        if churn >= 10 or collections >= 3:
            layer.score = max(layer.score or 0, 80)
        elif churn >= 4 or collections >= 1:
            layer.score = max(layer.score or 0, 50)


def _apply_late_tick_pressure(layers: list[LayerScore], update: dict[str, Any]) -> None:
    """Raise app_sim when the server missed its tick deadline this window."""
    late = int(update.get("lateTicks") or 0)
    window = int(update.get("windowUpdates") or 0)
    if not window:
        return
    late_share = late / window
    for layer in layers:
        if layer.layer != "app_sim" or layer.state != "collected":
            continue
        layer.signals["late_ticks"] = late
        layer.signals["late_tick_share"] = round(late_share, 4)
        layer.signals["tick_stall_ms"] = update.get("tickStallMsTotal")
        if late_share >= 0.25:
            layer.score = max(layer.score or 0, 90)
        elif late_share >= 0.05:
            layer.score = max(layer.score or 0, 60)


def _net_rates(io_net_text: str, seconds: float) -> dict[str, float]:
    """Windowed MB/s from the io_net.bt.out byte counters.

    UDP is 7DTD game traffic (chunk streaming); TCP is only telnet/http."""
    rates: dict[str, float] = {}
    for label, marker in (
        ("udp_send_mb_per_second", "udp_send_bytes"),
        ("udp_recv_mb_per_second", "udp_recv_bytes"),
        ("tcp_send_mb_per_second", "tcp_send_bytes"),
        ("tcp_recv_mb_per_second", "tcp_recv_bytes"),
    ):
        match = re.search(rf"@{marker}:\s*(\d+)", io_net_text)
        if match:
            rates[label] = round(int(match.group(1)) / 1048576 / seconds, 2)
    return rates


def _apply_memory_trend(layers: list[LayerScore], mem: dict[str, Any]) -> None:
    """Sustained RSS climb (not GC oscillation) = leak / unbounded buffer."""
    slope = float(mem.get("rss_growth_mb_per_s") or 0)
    fd_growth = int(mem.get("fd_end") or 0) - int(mem.get("fd_start") or 0)
    for layer in layers:
        if layer.layer != "memory_cache" or layer.state != "collected":
            continue
        layer.signals["rss_growth_mb_per_s"] = slope
        layer.signals["fd_growth"] = fd_growth
        if slope >= 5 or fd_growth >= 100:
            layer.score = max(layer.score or 0, 70)


def build_summary(session: Path) -> SummaryV2:
    """Score all layers and write summary.json (the only writer of that file)."""
    meta = _load_meta(session)
    texts = load_texts(session)
    hw = parse_perf_stat(texts.get("hw") or "")
    layers = layer_scores(session, hw, texts)

    threads = thread_summary(session)
    _apply_main_thread_pressure(layers, threads)

    measured = [layer for layer in layers if layer.score is not None]
    top = measured[0] if measured else None
    recommendation = (
        f"Focus on **{top.layer}** (pressure {top.score}). " + "; ".join(top.optimize[:2])
        if top
        else "No measured layer has sufficient evidence for a recommendation."
    )

    metadata: dict[str, Any] = {}
    snapshot_path = session / "app/apm_app.json"
    snapshot: dict[str, Any] | None = None
    # One read of the (forensic-sized) mono_alloc output feeds both the gross
    # churn fallback below and the site rankings further down; the annotated
    # variant preferred here carries the same counter lines (jitsym rewrites
    # only hex address tokens).
    alloc_text = _alloc_source_text(session)
    if snapshot_path.is_file():
        # A malformed bridge snapshot must not lose the host-side evidence
        # collected below; drop just the snapshot-derived blocks instead.
        try:
            loaded = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                snapshot = loaded
            metadata.update(_snapshot_metadata(snapshot or {}, alloc_text))
            if "gc" in metadata:
                _apply_gc_pressure(layers, metadata["gc"])
            _apply_late_tick_pressure(layers, (snapshot or {}).get("update") or {})
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError, OverflowError):
            # TypeError: a snapshot block whose numeric fields parsed as strings
            # or containers (hand-edited / imported) raises from the arithmetic
            # above (e.g. "16.6" - 3.2); drop just the snapshot-derived blocks,
            # exactly like a JSON decode failure. OverflowError: JSON "1e999"
            # parses to float('inf'), and int()/round() on it raises that, not
            # ValueError (models.as_number rejects inf for the same reason).
            # AttributeError: a block itself parsed as a non-object ("gc": [16.6])
            # has no .get - the same hazard attribute_snapshot documents.
            pass

    net_window = max(1.0, float(meta.get("seconds") or 1))
    net = _net_rates(texts.get("io_net", ""), net_window)
    if net:
        metadata["net"] = net
    # The alloc text read above also feeds both site rankings.
    metadata["top_alloc_sites"] = top_alloc_sites(session, text=alloc_text)
    metadata["top_churn_sites"] = top_churn_sites(session, text=alloc_text)
    metadata["cpu_hot_paths"] = top_cpu_hot_paths(session)
    mem = memory_trend(session)
    if mem:
        metadata["memory"] = mem
        _apply_memory_trend(layers, mem)
    # Subsystem attribution lets the verdict name a dominant managed subsystem
    # (e.g. network serialization at player scale) even when no host probe fired.
    # Compute it fresh from this session's bridge snapshot so the diagnosis is not
    # one finalize behind the bridge stage (which runs after summary). Reuses the
    # snapshot parsed above; only falls back to a re-read (or a prior
    # csharp_bridge.json) when that parse failed or the raw snapshot is absent.
    attribution: dict[str, Any] | None = None
    if snapshot is not None:
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            attribution = attribute_document(snapshot)
    if not attribution:
        attribution = attribute_snapshot(session)
    if not attribution:
        prior_bridge = session / "csharp_bridge.json"
        if prior_bridge.is_file():
            with contextlib.suppress(Exception):
                attribution = (
                    json.loads(prior_bridge.read_text(encoding="utf-8")).get("attribution") or {}
                )
    metadata["lag_diagnosis"] = diagnose_lag(layers, metadata, threads, attribution, session)

    perf_dir = session / "cpu" / "perf"
    flames = {
        "interactive": "cpu/perf/flame.html" if (perf_dir / "flame.html").exists() else None,
        "speedscope": "cpu/perf/profile.speedscope.json"
        if (perf_dir / "profile.speedscope.json").exists()
        else None,
        "svg": "cpu/perf/flame.svg" if (perf_dir / "flame.svg").exists() else None,
        "folded": "cpu/perf/stacks.folded" if (perf_dir / "stacks.folded").exists() else None,
    }
    summary = SummaryV2(
        session_id=session.name,
        metadata=metadata,
        meta=meta,
        hw=hw,
        layers=layers,
        threads=threads,
        recommendation=recommendation,
        flames=flames,
        files={
            "futex": "sync/futex.bt.out",
            "vfs": "io/vfs.bt.out",
            "block": "io/block.bt.out",
            "hw_stat": "memory/hw_stat.txt",
            "flame_html": "cpu/perf/flame.html",
            "speedscope": "cpu/perf/profile.speedscope.json",
            "mono_gc": "runtime/mono_gc.bt.out",
        },
    )
    atomic_json(session / "summary.json", schema_dict(summary))
    return summary
