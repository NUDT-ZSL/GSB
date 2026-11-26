"""Typed configuration and normalization result models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Category:
    id: int
    name: str


@dataclass(frozen=True)
class InstanceMapping:
    instance_id: int
    category_id: int
    confidence_threshold: float
    effective_at_ms: int
    confidence_scope: str = "implicit_one"


@dataclass(frozen=True)
class MigrationAction:
    action: str
    target: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class MigrationReport:
    direction: Literal["v1_to_v2", "v2_to_v1"]
    policy: str
    actions: list[MigrationAction] = field(default_factory=list)
    preserved_pixel_checksum: str | None = None

    def add(self, action: str, target: str, **detail: Any) -> None:
        self.actions.append(MigrationAction(action, target, detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "policy": self.policy,
            "preserved_pixel_checksum": self.preserved_pixel_checksum,
            "action_count": len(self.actions),
            "actions": [asdict(action) for action in self.actions],
        }


@dataclass
class UnifiedConfig:
    source_version: Literal[1, 2]
    revision: int
    categories: tuple[Category, ...]
    mappings: tuple[InstanceMapping, ...]
    default_confidence_threshold: float
    effective_at_ms: int
    relative_effective_seconds: float | None = None
    time_origin_ms: int = 0
    downgrade_policy: str = "discard"
    mapping_source: str = "explicit"
    checksum: str = ""
    raw_digest: str = ""

    def category_ids(self) -> set[int]:
        return {item.id for item in self.categories}

    def mapping_for(self, instance_id: int) -> InstanceMapping | None:
        for mapping in self.mappings:
            if mapping.instance_id == int(instance_id):
                return mapping
        return None

    def threshold_for(self, instance_id: int) -> float:
        mapping = self.mapping_for(instance_id)
        if mapping is None:
            return self.default_confidence_threshold
        return float(mapping.confidence_threshold)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": "seg-diag-unified-config/v1",
            "source_version": self.source_version,
            "revision": self.revision,
            "categories": [asdict(item) for item in sorted(self.categories, key=lambda x: x.id)],
            "mappings": [
                asdict(item)
                for item in sorted(self.mappings, key=lambda x: x.instance_id)
            ],
            "default_confidence_threshold": self.default_confidence_threshold,
            "effective_at_ms": self.effective_at_ms,
            "relative_effective_seconds": self.relative_effective_seconds,
            "time_origin_ms": self.time_origin_ms,
            "downgrade_policy": self.downgrade_policy,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.canonical_payload()
        payload["checksum"] = self.checksum
        payload["raw_digest"] = self.raw_digest
        payload["mapping_source"] = self.mapping_source
        return payload
