"""Unified event timeline from APM collector outputs.

Parses SLOW_* lines, managed bridge spikes, thread wchan snapshots, and proc
samples into events.json/events.jsonl. Raw event materialization is bounded
per source; aggregate counts always cover every parsed event.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..io import atomic_json, atomic_text
from ..models import EventsV2, as_number, schema_dict

RETAINED_MAX = 2000
PER_SOURCE_MAX = 500

# Non-reset count() aggregators printed once per interval as growing cumulative
# lines ("@wait_n: 40"); the LAST occurrence is the true total.
_COUNTER_PATTERNS = (
    (r"@wait_n:\s*(\d+)", "futex_waits"),
    (r"@little_n:\s*(\d+)", "gc_little"),
    (r"@gc_n:\s*(\d+)", "gc_collect"),
)


class EventSink:
    """Counts every event but materializes at most PER_SOURCE_MAX per source."""

    def __init__(self) -> None:
        self.count = 0
        self.by_kind: dict[str, int] = {}
        self.events: list[dict[str, Any]] = []
        self._per_source: dict[str, int] = {}

    def add(self, event: dict[str, Any]) -> None:
        self.count += 1
        kind = str(event.get("kind") or "unknown")
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
        source = str(event.get("source") or "")
        seen = self._per_source.get(source, 0)
        if seen >= PER_SOURCE_MAX:
            return
        self._per_source[source] = seen + 1
        self.events.append(event)


def parse_bt_slow(sink: EventSink, path: Path, kind: str) -> None:
    if not path.is_file():
        return
    # One streamed pass instead of read-all + splitlines + three extra
    # full-text regex scans; per-line matching is equivalent because bpftrace
    # prints each map record on a single line.
    counters = [re.compile(pattern) for pattern, _ in _COUNTER_PATTERNS]
    counter_last: list[str | None] = [None] * len(counters)
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for i, line in enumerate(stream):
            if "SLOW_" in line or "SLOW " in line or "STALL_MAIN" in line or "STW_PAUSE" in line:
                sink.add(
                    {
                        "t": None,
                        "kind": kind,
                        "severity": "warn",
                        "message": line.strip()[:300],
                        "source": path.name,
                        "line": i + 1,
                    }
                )
            for j, pattern in enumerate(counters):
                match = pattern.search(line)
                if match:
                    counter_last[j] = match.group(1)
    for (_pattern, label), total in zip(_COUNTER_PATTERNS, counter_last, strict=True):
        # The value is cumulative and printed every interval, so the last is
        # the true total (same contract as the former findall()[-1]).
        if total is not None:
            sink.add(
                {
                    "t": None,
                    "kind": "counter",
                    "severity": "info",
                    "message": f"{label}={total}",
                    "source": path.name,
                    "value": int(total),
                }
            )


def parse_proc_jsonl(sink: EventSink, path: Path) -> None:
    if not path.is_file():
        return
    prev_rss: float | None = None
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = record.get("t")
            cpu = as_number(record.get("cpu_pct"))
            rss = as_number(record.get("rss_mb"))
            if cpu is not None and cpu > 150:
                sink.add(
                    {
                        "t": t,
                        "kind": "cpu_spike",
                        "severity": "warn",
                        "message": f"process cpu%={cpu:.0f} rssMB={rss}",
                        "source": "proc.jsonl",
                        "value": cpu,
                    }
                )
            if rss is not None and prev_rss is not None and rss - prev_rss > 50:
                sink.add(
                    {
                        "t": t,
                        "kind": "rss_jump",
                        "severity": "warn",
                        "message": f"RSS jump {prev_rss:.0f}→{rss:.0f} MB",
                        "source": "proc.jsonl",
                        "value": rss - prev_rss,
                    }
                )
            if rss is not None:
                prev_rss = rss


def parse_threads_jsonl(sink: EventSink, path: Path) -> None:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            wchan = record.get("wchan_top") or {}
            for name, raw in list(wchan.items())[:5]:
                waiters = as_number(raw)
                if (
                    waiters is not None
                    and waiters >= 3
                    and name not in ("0", "-", "0x0")
                    and ("futex" in name.lower() or "wait" in name.lower())
                ):
                    sink.add(
                        {
                            "t": record.get("t"),
                            "kind": "wchan",
                            "severity": "info",
                            "message": f"wchan {name}×{int(waiters)}",
                            "source": "threads.jsonl",
                            "value": int(waiters),
                        }
                    )


def parse_app_scrape(sink: EventSink, path: Path) -> None:
    if not path.is_file():
        return
    # Streamed like every other bridge.jsonl reader (report.load_texts): each
    # record carries a whole telnet reply, so the file can reach tens of MB.
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(record.get("text") or "")
            t = record.get("t")
            if "spike" in text.lower():
                # The telnet drain interleaves server log lines (player names, IPs,
                # Steam IDs) with bridge output; embed only the extracted duration,
                # never the raw console text. bridge.jsonl stays the owner-only
                # evidence store and is excluded from export bundles.
                match = re.search(r"gmUpdateDuration=([\d.]+)ms", text)
                duration = f"{float(match.group(1)):.1f}ms" if match else None
                sink.add(
                    {
                        "t": t,
                        "kind": "managed_bridge_spike",
                        "severity": "error",
                        "message": (
                            f"managed bridge spike gmUpdateDuration={duration}"
                            if duration
                            else "managed bridge spike (raw console text withheld; see app/bridge.jsonl)"
                        ),
                        "source": "bridge.jsonl",
                        **({"value": float(match.group(1))} if match else {}),
                    }
                )
            match = re.search(r"avg=([\d.]+)ms", text)
            if match and float(match.group(1)) >= 33:
                sink.add(
                    {
                        "t": t,
                        "kind": "managed_bridge_slow_update",
                        "severity": "warn",
                        "message": f"managed bridge update avg={match.group(1)}ms",
                        "source": "bridge.jsonl",
                        "value": float(match.group(1)),
                    }
                )


def parse_bridge_spikes(sink: EventSink, path: Path) -> None:
    """Frame spikes recorded in-game by the bridge (utc-stamped, exact durations)."""
    if not path.is_file():
        return
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return
    for spike in snapshot.get("spikes") or []:
        # spikes[] sits outside BridgeSnapshotV3 validation (extra="allow"), so
        # a format-changed or hand-edited record must coerce like every other
        # collector field instead of raising float(TypeError) mid-timeline.
        duration = as_number(spike.get("gmUpdateDurationMs")) or 0.0
        tick_ms = as_number(spike.get("serverTickIntervalMs")) or 0.0
        epoch: float | None = None
        with contextlib.suppress(ValueError, TypeError):
            # Naive stamps (no offset) are UTC by repo convention, matching
            # session._date and capture._ingest_bridge_snapshot: a bare
            # .timestamp() would resolve them in this host's local zone and
            # shift every spike on a non-UTC analysis host.
            stamp = datetime.fromisoformat(str(spike.get("utc") or "").replace("Z", "+00:00"))
            epoch = (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp()
        sink.add(
            {
                "t": epoch,
                "kind": "frame_spike",
                "severity": "error" if duration >= 200 else "warn",
                "message": (
                    f"gmUpdate {duration:.1f}ms tickInterval {tick_ms:.1f}ms "
                    f"entities={((spike.get('world') or {}).get('entities'))}"
                ),
                "source": "apm_app.json",
                "value": duration,
            }
        )


def build_timeline(session: Path) -> EventsV2:
    sink = EventSink()
    parse_bridge_spikes(sink, session / "app/apm_app.json")
    parse_bt_slow(sink, session / "sync/futex.bt.out", "futex")
    parse_bt_slow(sink, session / "io/block.bt.out", "block_io")
    parse_bt_slow(sink, session / "io/vfs.bt.out", "vfs_io")
    parse_bt_slow(sink, session / "runtime/mono_gc.bt.out", "gc")
    parse_proc_jsonl(sink, session / "memory/proc.jsonl")
    parse_threads_jsonl(sink, session / "threads/threads.jsonl")
    parse_app_scrape(sink, session / "app/bridge.jsonl")

    def _t(event: dict[str, Any]) -> float | None:
        # A non-numeric "t" (corrupt or format-changed collector record) must not
        # crash the whole timeline build; treat it as untimed instead.
        try:
            value = event.get("t")
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    timed = [e for e in sink.events if _t(e) is not None]
    untimed = [e for e in sink.events if _t(e) is None]
    timed.sort(key=lambda e: _t(e) or 0.0)
    retained = (timed + untimed)[:RETAINED_MAX]

    return EventsV2.model_validate(
        {
            "session": session.name,
            "count": sink.count,
            "retained": len(retained),
            "dropped": sink.count - len(retained),
            "by_kind": sink.by_kind,
            "events": retained,
        }
    )


def build_events(session: Path) -> EventsV2:
    """Build and persist events.json + events.jsonl."""
    doc = build_timeline(session)
    atomic_json(session / "events.json", schema_dict(doc))
    atomic_text(
        session / "events.jsonl",
        "".join(json.dumps(event.model_dump(mode="json")) + "\n" for event in doc.events),
    )
    return doc
