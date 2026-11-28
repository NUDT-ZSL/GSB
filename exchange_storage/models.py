"""Data models shared by parsing, planning, and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


HEALTH_STATES = frozenset({"healthy", "degraded", "offline"})
EVENT_TYPES = frozenset({"access", "expire"})
INCIDENT_TYPES = frozenset(
    {
        "health_degraded",
        "health_change",
        "watermark_change",
        "watermark_exceeded",
    }
)


@dataclass(frozen=True)
class Policy:
    watermark_ratio: float
    max_latency_ms: float
    urgency_weights: dict[str, float]


@dataclass(frozen=True)
class Institution:
    id: str
    name: str
    required_compliance_level: int
    max_latency_ms: float


@dataclass
class Region:
    id: str
    name: str
    capacity_bytes: int
    baseline_used_bytes: int
    health: str
    latency_ms: float
    compliance_level: int
    watermark_ratio: float


@dataclass
class StorageObject:
    id: str
    institution_id: str
    test_category: str
    urgency: str
    bytes: int
    created_at: float
    expires_at: float
    digest: str
    reference_count: int


@dataclass(frozen=True)
class TimelineEvent:
    at: float
    type: str
    object_id: str
    delta: int
    index: int


@dataclass(frozen=True)
class Incident:
    at: float
    type: str
    region_id: str
    health: str | None
    watermark_ratio: float | None
    after_migration_step: int | None
    index: int


@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str
    path: str = "$"
    object_id: str | None = None
    institution_id: str | None = None
    region_id: str | None = None
    event_index: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }
        if self.object_id is not None:
            result["object_id"] = self.object_id
        if self.institution_id is not None:
            result["institution_id"] = self.institution_id
        if self.region_id is not None:
            result["region_id"] = self.region_id
        if self.event_index is not None:
            result["event_index"] = self.event_index
        return result


@dataclass
class Project:
    project_id: str
    policy: Policy
    institutions: dict[str, Institution]
    regions: dict[str, Region]
    objects: dict[str, StorageObject]
    events: list[TimelineEvent]
    incidents: list[Incident]
    source_seed: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def validation_report(errors: list[ValidationError]) -> dict[str, Any]:
    return {
        "valid": not errors,
        "error_count": len(errors),
        "errors": [error.as_dict() for error in errors],
    }
