from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def as_number(value: Any) -> float | None:
    """Coerce an unvalidated JSON scalar to a finite float, else None.

    Session documents are re-read without schema guarantees (hand-edited,
    older writers, imported bundles): a string like "abc" or JSON 1e999
    (which parses to inf) must degrade to "no data" instead of raising
    ValueError/OverflowError mid-analysis. bools are rejected even though
    float() accepts them - True is not a measurement.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def as_mapping(value: Any) -> dict[str, Any]:
    """Coerce an unvalidated JSON value to an object, else {}.

    Same posture as as_number: session documents and budget files are re-read
    without schema guarantees (imported bundles, hand edits), so a scalar or
    list where an object is expected ("metadata": 5) must read as absent
    evidence instead of raising AttributeError/TypeError mid-analysis.
    """
    return dict(value) if isinstance(value, dict) else {}


def object_list(value: Any) -> list[dict[str, Any]]:
    """Coerce an unvalidated JSON value to a list of objects, dropping the rest.

    The list-shaped sibling of as_mapping: session documents and bridge outputs
    are re-read without schema guarantees (hand-edited, older writers, imported
    bundles), so a scalar or object where a list of records belongs
    ("layers": {...}, a list holding a bare string) must read as absent records
    instead of iterating a dict's keys or raising AttributeError on the first
    non-object element. Same posture as as_number/as_mapping: unreadable shape
    is missing evidence, never a crash of the reader.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def first_number(*values: Any) -> float | None:
    """First value coercible to a finite number, else None.

    Unlike `a or b`, a legitimate 0 is kept rather than falling through to the
    next field; an unparseable value (imported/hand-edited JSON) is skipped
    like a missing one.
    """
    for value in values:
        number = as_number(value)
        if number is not None:
            return number
    return None


def first_present(*values: Any) -> float:
    """first_number with a 0.0 floor, for readers that need a number."""
    return first_number(*values) or 0.0


# Dedicated-server process name prefix shared by every Python autodetector
# (capture --pid resolution, doctor's candidate report).
SERVER_COMM = "7DaysToDieServe"

# Status of one collector for one session. Exported so the writers in capture
# (_result) and the session audit (which rewrites unavailable -> skipped) pass
# a checked value instead of a bare str the model layer has to re-narrow.
CollectorStatus = Literal["ok", "skipped", "failed", "unavailable", "interrupted"]
HealthGrade = Literal["A", "B", "C", "D", "F"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(StrictModel):
    pid: int = Field(gt=0)
    comm: str = SERVER_COMM
    exe: str = ""
    cmdline: str = ""


class CollectorResult(StrictModel):
    schema_: Literal["7dtd.apm.collector-result.v1"] = Field(
        default="7dtd.apm.collector-result.v1", alias="schema"
    )
    name: str
    layer: str
    status: CollectorStatus
    exit_code: int | None = None
    duration_seconds: float = Field(default=0, ge=0)
    tool: str = ""
    tool_version: str = ""
    sample_count: int | None = Field(default=None, ge=0)
    artifacts: list[str] = Field(default_factory=list)
    message: str = ""


class Artifact(StrictModel):
    path: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManifestV2(StrictModel):
    schema_: Literal["7dtd.apm.manifest.v2"] = Field(default="7dtd.apm.manifest.v2", alias="schema")
    session_id: str
    # A session whose meta.json carries no usable stamp records an unknown
    # start rather than the wall clock of whoever last audited it.
    started_at: datetime | None = None
    ended_at: datetime | None = None
    target: Target
    requested_layers: list[str]
    collectors: list[CollectorResult] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class LayerScore(StrictModel):
    layer: str
    score: float | None = Field(default=None, ge=0, le=100)
    state: Literal["collected", "skipped", "failed", "unavailable"] = "collected"
    confidence: Literal["none", "low", "medium", "high"] = "low"
    signals: dict[str, Any] = Field(default_factory=dict)
    optimize: list[str] = Field(default_factory=list)


class SummaryV2(StrictModel):
    schema_: Literal["7dtd.apm.summary.v2"] = Field(default="7dtd.apm.summary.v2", alias="schema")
    session_id: str
    layers: list[LayerScore]
    recommendation: str = ""
    health: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    hw: dict[str, float] = Field(default_factory=dict)
    threads: dict[str, Any] = Field(default_factory=dict)
    flames: dict[str, str | None] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)


class MetaV2(StrictModel):
    schema_: Literal["7dtd.apm.session.v2"] = Field(default="7dtd.apm.session.v2", alias="schema")
    utc: datetime
    pid: int = Field(gt=0)
    comm: str = SERVER_COMM
    seconds: int = Field(gt=0)
    # The window the collectors actually ran, recorded only when the capture
    # ended before `seconds` elapsed (Ctrl-C). `seconds` stays the requested
    # window; readers that divide by time must use effective_seconds() so a
    # truncated capture cannot pass as a full-length one.
    observed_seconds: float | None = Field(default=None, ge=0)
    only: str = "all"
    no_app: bool = False
    exe: str = ""
    cmdline: str = ""
    threads: int = Field(default=0, ge=0)
    uname: str = ""
    analyzer_version: str = ""
    capture_preset: str = ""
    layers: list[str] = Field(default_factory=list)
    tool_versions: dict[str, str] = Field(default_factory=dict)


class EventV2(BaseModel):
    # extra="allow": an imported bundle may carry collector keys this writer
    # does not know, and an unknown key must not fail an audit. The fields the
    # readers below actually consume are declared, not left to the extras, so
    # the type checker sees them; defaults keep every document that validated
    # before still validating.
    model_config = ConfigDict(extra="allow")
    kind: str
    severity: Literal["info", "warn", "error"]
    message: str
    # EventSink bounds retention per source (PER_SOURCE_MAX), so readers and
    # the type checker both need the field.
    source: str = ""
    # Wall-clock epoch seconds, None for a collector event the probe printed
    # without one (the bpftrace SLOW_* lines). The timeline sorts on it and the
    # bridge stall correlation windows on it.
    t: float | None = None
    # The measured magnitude: spike duration ms, waiters, cpu%, RSS delta.
    value: float | None = None


class EventsV2(StrictModel):
    schema_: Literal["7dtd.apm.events.v2"] = Field(default="7dtd.apm.events.v2", alias="schema")
    session: str
    count: int = Field(ge=0)
    retained: int = Field(ge=0)
    dropped: int = Field(ge=0)
    by_kind: dict[str, int] = Field(default_factory=dict)
    events: list[EventV2] = Field(default_factory=list)

    @model_validator(mode="after")
    def _counts_consistent(self) -> EventsV2:
        # CHECK-constraint analog: count is the total observed, retained the
        # materialized subset, dropped the remainder. A document violating
        # either identity is corrupt or hand-edited and must fail validation
        # instead of feeding readers a silently inconsistent timeline.
        if self.retained != len(self.events):
            raise ValueError(
                f"retained={self.retained} does not match {len(self.events)} materialized events"
            )
        if self.count != self.retained + self.dropped:
            raise ValueError(
                f"count={self.count} != retained {self.retained} + dropped {self.dropped}"
            )
        return self


class HealthV2(StrictModel):
    schema_: Literal["7dtd.apm.health.v2"] = Field(default="7dtd.apm.health.v2", alias="schema")
    session: str = ""
    health: float | None = Field(default=None, ge=0, le=100)
    pressure: float | None = Field(default=None, ge=0, le=100)
    grade: HealthGrade | None = None
    coverage: float | None = Field(default=None, ge=0, le=1)
    confidence: Literal["insufficient", "medium"] = "insufficient"
    reason: str = ""
    detail: dict[str, dict[str, float]] = Field(default_factory=dict)


class ManagedSectionV3(StrictModel):
    name: str
    calls: int = Field(ge=0)
    avgMs: float = Field(ge=0)
    lastMs: float = Field(ge=0)
    maxMs: float = Field(ge=0)
    p50Ms: float = Field(ge=0)
    p95Ms: float = Field(ge=0)
    p99Ms: float = Field(ge=0)
    totalMs: float = Field(ge=0)
    deep: bool = False


class BridgeSnapshotV3(BaseModel):
    """Typed boundary for game-owned fields while retaining versioned extension data."""

    model_config = ConfigDict(extra="allow")
    schema_: Literal["7dtd.apm.app.v3"] = Field(alias="schema")
    provider: Literal["7dtd-server-apm-bridge"]
    providerVersion: str
    utc: datetime
    sections: list[ManagedSectionV3]


def schema_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


# Request tokens accepted for each canonical layer beyond the layer name itself.
# One table consumed by capture planning and the audit through
# collector_requested() over the shared catalog (collectors.SPECS), and by
# summary scoring (report.layer_scores), so they cannot drift into disagreeing
# about what an --only token means.
LAYER_ALIASES: dict[str, frozenset[str]] = {
    "app_sim": frozenset({"app"}),
    "io": frozenset({"net"}),
    "memory_cache": frozenset({"memory", "hw", "cache", "proc"}),
    "runtime_gc": frozenset({"runtime", "gc"}),
    "scheduler": frozenset({"sched"}),
    "sync_locks": frozenset({"sync", "locks", "futex"}),
}


def layer_requested(layer: str, requested: set[str]) -> bool:
    """True when a capture requested this layer by name, alias, or "all"."""
    return bool(
        "all" in requested
        or layer in requested
        or LAYER_ALIASES.get(layer, frozenset()) & requested
    )


def collector_requested(
    name: str,
    layer: str,
    requested: set[str],
    *,
    extra_aliases: frozenset[str] = frozenset(),
    optin: bool = False,
) -> bool:
    """Single resolution rule for --only tokens against one collector.

    Shared by capture planning and the session audit so the two cannot drift
    into disagreeing about what a token means (the audit flips unplanned
    collectors to "skipped" and flags planned-but-empty ones; a disagreement
    here surfaces as false or missing warnings in every manifest). A collector
    is requested when the token names it directly (collector name or an
    extra_alias) or names its layer (layer name or a LAYER_ALIASES entry).
    Opt-in collectors answer only their own name/alias: never "all" nor a
    layer token, because they are deliberately excluded from standard plans.
    """
    if optin:
        return bool(name in requested or extra_aliases & requested)
    return bool(layer_requested(layer, requested) or name in requested or extra_aliases & requested)


def effective_seconds(meta: Mapping[str, Any]) -> float:
    """The capture window the collectors actually ran, in seconds.

    `seconds` is the REQUESTED window, and a capture cut short (Ctrl-C) still
    records it. Dividing by it would understate every rate the window
    produced (futex stalls/s, net MB/s) and let a 10s truncated capture pass
    `compare`'s duration gate against a 60s one as if both ran 60s. The
    observed window recorded at capture end wins whenever it is shorter;
    older sessions have no such field and fall back to the requested value.
    """
    requested = as_number(meta.get("seconds")) or 0.0
    observed = as_number(meta.get("observed_seconds"))
    if observed is not None and 0.0 < observed < requested:
        return observed
    return requested


def layer_is_collected(layer: Mapping[str, Any]) -> bool:
    """True when a summary layer entry carries evidence.

    A missing `state` means collected, matching the LayerScore default, so
    sessions written before the field existed score instead of vanishing from
    health, budget, compare and the Prometheus export alike.
    """
    return str(layer.get("state", "collected")) == "collected"


def collected_layer_scores(summary: Mapping[str, Any]) -> dict[str, float]:
    """layer name -> pressure score for layers with state "collected" and a score.

    Summary JSON is unvalidated on read paths (hand-edited or older sessions),
    so shape is checked here rather than assumed.
    """
    out: dict[str, float] = {}
    for layer in object_list(summary.get("layers")):
        name = layer.get("layer")
        pressure = as_number(layer.get("score"))
        if name and layer_is_collected(layer) and pressure is not None:
            out[str(name)] = pressure
    return out


def layer_signals(summary: Mapping[str, Any], layer_name: str) -> dict[str, Any]:
    """signals mapping of one summary layer entry ({} when absent or malformed).

    Unlike collected_layer_scores this ignores `state`: readers want the
    recorded signals (e.g. runtime_gc STW pauses) even from a failed layer.
    Summary JSON is re-read without schema guarantees (hand-edited, imported,
    older writers), so the lookup tolerates any shape instead of assuming
    layers[] entries are objects.
    """
    for layer in object_list(summary.get("layers")):
        if layer.get("layer") == layer_name:
            signals = layer.get("signals")
            return signals if isinstance(signals, dict) else {}
    return {}
