from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(StrictModel):
    pid: int = Field(gt=0)
    comm: str = "7DaysToDieServe"
    exe: str = ""
    cmdline: str = ""


class CollectorResult(StrictModel):
    schema_: Literal["7dtd.apm.collector-result.v1"] = Field(
        default="7dtd.apm.collector-result.v1", alias="schema"
    )
    name: str
    layer: str
    status: Literal["ok", "skipped", "failed", "unavailable", "interrupted"]
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
    started_at: datetime
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
    comm: str = "7DaysToDieServe"
    seconds: int = Field(gt=0)
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
    model_config = ConfigDict(extra="allow")
    kind: str
    severity: Literal["info", "warn", "error"]
    message: str


class EventsV2(StrictModel):
    schema_: Literal["7dtd.apm.events.v2"] = Field(default="7dtd.apm.events.v2", alias="schema")
    session: str
    count: int = Field(ge=0)
    retained: int = Field(ge=0)
    dropped: int = Field(ge=0)
    by_kind: dict[str, int] = Field(default_factory=dict)
    events: list[EventV2] = Field(default_factory=list)


class HealthV2(StrictModel):
    schema_: Literal["7dtd.apm.health.v2"] = Field(default="7dtd.apm.health.v2", alias="schema")
    session: str = ""
    health: float | None = Field(default=None, ge=0, le=100)
    pressure: float | None = Field(default=None, ge=0, le=100)
    grade: Literal["A", "B", "C", "D", "F"] | None = None
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
    provider: Literal["7dtd-apm-bridge"]
    providerVersion: str
    utc: datetime
    sections: list[ManagedSectionV3]


def schema_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


def relative_artifact(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
