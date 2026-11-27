"""Shared data structures and deterministic serialization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Iterable


def stable_uint(value: str, bits: int = 64) -> int:
    """Return a deterministic unsigned integer derived from UTF-8 text."""
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[: bits // 8], "big")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def partition_id(topic: str, partition: int) -> str:
    return f"{topic}:{partition}"


@dataclass(frozen=True)
class MemberSpec:
    member_id: str
    processing_rate: float
    session_timeout_ms: int
    subscriptions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "processing_rate": self.processing_rate,
            "session_timeout_ms": self.session_timeout_ms,
            "subscriptions": list(self.subscriptions),
        }


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "partitions": list(self.partitions)}


@dataclass(frozen=True)
class Provenance:
    field: str
    source_path: str
    source_schema: str
    raw_value: Any
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "source_path": self.source_path,
            "source_schema": self.source_schema,
            "raw_value": self.raw_value,
            "note": self.note,
        }


@dataclass
class NormalizedProject:
    group_id: str
    members: dict[str, MemberSpec]
    topics: dict[str, TopicSpec]
    provenance: list[Provenance] = field(default_factory=list)
    source_schema: str = ""

    def content_dict(self) -> dict[str, Any]:
        """Canonical content only; provenance is intentionally excluded."""
        return {
            "group_id": self.group_id,
            "members": [
                self.members[member_id].as_dict()
                for member_id in sorted(self.members)
            ],
            "topics": [
                self.topics[name].as_dict() for name in sorted(self.topics)
            ],
        }

    def as_dict(self, include_provenance: bool = True) -> dict[str, Any]:
        result = self.content_dict()
        result["source_schema"] = self.source_schema
        if include_provenance:
            result["provenance"] = sorted(
                (entry.as_dict() for entry in self.provenance),
                key=lambda item: (item["field"], item["source_path"]),
            )
        return result

    def content_sha256(self) -> str:
        return canonical_sha256(self.content_dict())


@dataclass(frozen=True)
class MemberEvent:
    event_id: str
    at_ms: int
    kind: str
    member_id: str
    rate: float | None = None
    source_index: int = 0

    @classmethod
    def from_dict(cls, value: dict[str, Any], index: int = 0) -> "MemberEvent":
        kind = str(value.get("type", value.get("kind", "")))
        event_id = str(value.get("event_id", f"member-event-{index:04d}"))
        rate_value = value.get("rate", value.get("processing_rate"))
        return cls(
            event_id=event_id,
            at_ms=int(value["at_ms"]),
            kind=kind,
            member_id=str(value["member_id"]),
            rate=float(rate_value) if rate_value is not None else None,
            source_index=index,
        )


@dataclass(frozen=True)
class KeyedEvent:
    event_id: str
    topic: str
    partition: int
    offset: int
    partition_key: str
    available_at_ms: int
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "KeyedEvent":
        return cls(
            event_id=str(value["event_id"]),
            topic=str(value["topic"]),
            partition=int(value["partition"]),
            offset=int(value["offset"]),
            partition_key=str(value["partition_key"]),
            available_at_ms=int(value.get("available_at_ms", 0)),
            event_type=str(value.get("event_type", "")),
            payload=dict(value.get("payload", {})),
        )

    @property
    def pid(self) -> str:
        return partition_id(self.topic, self.partition)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "topic": self.topic,
            "partition": self.partition,
            "offset": self.offset,
            "partition_key": self.partition_key,
            "available_at_ms": self.available_at_ms,
            "event_type": self.event_type,
            "payload": self.payload,
        }


def partition_for_key(topic: str, key: str, partition_count: int) -> int:
    if partition_count <= 0:
        raise ValueError(f"topic {topic!r} has no partitions")
    return stable_uint(f"{topic}\x1f{key}") % partition_count


def sorted_grouped(items: Iterable[KeyedEvent]) -> dict[str, list[KeyedEvent]]:
    grouped: dict[str, list[KeyedEvent]] = {}
    for event in items:
        grouped.setdefault(event.pid, []).append(event)
    for events in grouped.values():
        events.sort(key=lambda item: item.offset)
    return grouped
