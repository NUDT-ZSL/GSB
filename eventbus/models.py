"""Domain models and their canonical configuration representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldDef:
    name: str
    type: str = "any"
    default: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "default": self.default}


@dataclass
class ChannelDef:
    name: str
    generation: int
    partitions: int
    field_schema: list[FieldDef]
    aliases: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "generation": self.generation,
            "partitions": self.partitions,
            "field_schema": [item.to_dict() for item in self.field_schema],
            "aliases": sorted(set(self.aliases)),
            "sources": sorted(set(self.sources)),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelDef":
        return cls(
            name=data["name"],
            generation=int(data["generation"]),
            partitions=int(data["partitions"]),
            field_schema=[FieldDef(**item) for item in data["field_schema"]],
            aliases=list(data.get("aliases", [])),
            sources=list(data.get("sources", [])),
        )


@dataclass
class BridgeDef:
    id: str
    source: str
    target: str
    source_generation: int
    target_generation: int
    min_upstream_watermark: int
    filter: dict[str, Any]
    transforms: list[dict[str, Any]]
    max_batch: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "source_generation": self.source_generation,
            "target_generation": self.target_generation,
            "min_upstream_watermark": self.min_upstream_watermark,
            "filter": self.filter,
            "transforms": self.transforms,
            "max_batch": self.max_batch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BridgeDef":
        return cls(**data)


@dataclass
class SubscriptionDef:
    name: str
    channel: str
    start_offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "channel": self.channel,
            "start_offset": self.start_offset,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubscriptionDef":
        return cls(**data)


@dataclass
class UnifiedEvent:
    channel: str
    sequence: int
    partition_key: str
    logical_clock: int
    produced_at_ms: int
    fields: dict[str, Any]
    tombstone: bool
    source_version: str
    source_generation: int
    raw_repr: dict[str, Any]
    time_conversion: dict[str, Any] | None
    decision_basis: str
    decision_options: list[dict[str, Any]]
    degradations: list[dict[str, Any]]
    field_checksum: str


@dataclass
class Config:
    channels: dict[str, ChannelDef]
    bridges: dict[str, BridgeDef]
    subscriptions: dict[str, SubscriptionDef]
    retention_bytes: int
    clock_tolerance_ms: int
    source_fingerprint: str = ""
    base_epoch_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "eventbus-config-v1",
            "channels": {
                name: self.channels[name].to_dict() for name in sorted(self.channels)
            },
            "bridges": {
                bridge_id: self.bridges[bridge_id].to_dict()
                for bridge_id in sorted(self.bridges)
            },
            "subscriptions": {
                name: self.subscriptions[name].to_dict()
                for name in sorted(self.subscriptions)
            },
            "retention_bytes": self.retention_bytes,
            "clock_tolerance_ms": self.clock_tolerance_ms,
            "source_fingerprint": self.source_fingerprint,
            "base_epoch_ms": self.base_epoch_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        return cls(
            channels={
                name: ChannelDef.from_dict(item)
                for name, item in data["channels"].items()
            },
            bridges={
                bridge_id: BridgeDef.from_dict(item)
                for bridge_id, item in data["bridges"].items()
            },
            subscriptions={
                name: SubscriptionDef.from_dict(item)
                for name, item in data["subscriptions"].items()
            },
            retention_bytes=int(data["retention_bytes"]),
            clock_tolerance_ms=int(data["clock_tolerance_ms"]),
            source_fingerprint=data.get("source_fingerprint", ""),
            base_epoch_ms=int(data.get("base_epoch_ms", 0)),
        )


@dataclass
class LoadedDataset:
    config: Config
    events: list[UnifiedEvent]
    merge_decisions: list[dict[str, Any]]
    degradations: list[dict[str, Any]]
    source_fingerprint: str
