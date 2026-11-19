"""Internal value objects shared by parsing, merge, and lag computation."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any


@dataclass(frozen=True)
class PositionRecord:
    tx_id: str
    tx_num: int
    source_version: str
    source_priority: int
    logical_clock: int
    replay_generation: int
    source_node: str
    replica_node: str
    committed: bool
    payload_bytes: int
    commit_time_ms: int
    replay_time_ms: int
    delay_ms: int
    tombstone_applied: bool
    original_time: dict[str, Any]
    original_delay: dict[str, Any]
    migration: dict[str, Any] | None
    error_upper_bound: Fraction
    error_sources: tuple[str, ...]
    location: str = ""
    content_digest: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "tx_num": self.tx_num,
            "source_version": self.source_version,
            "source_priority": self.source_priority,
            "logical_clock": self.logical_clock,
            "replay_generation": self.replay_generation,
            "source_node": self.source_node,
            "replica_node": self.replica_node,
            "committed": self.committed,
            "payload_bytes": self.payload_bytes,
            "commit_time_ms": self.commit_time_ms,
            "replay_time_ms": self.replay_time_ms,
            "delay_ms": self.delay_ms,
            "tombstone_applied": self.tombstone_applied,
            "original_time": self.original_time,
            "original_delay": self.original_delay,
            "migration": self.migration,
            "error_upper_bound": _fraction_json(self.error_upper_bound),
            "error_upper_bound_exact": _fraction_text(self.error_upper_bound),
            "error_sources": list(self.error_sources),
            "location": self.location,
            "content_digest": self.content_digest,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PositionRecord":
        return cls(
            tx_id=data["tx_id"],
            tx_num=int(data["tx_num"]),
            source_version=data["source_version"],
            source_priority=int(data["source_priority"]),
            logical_clock=int(data["logical_clock"]),
            replay_generation=int(data["replay_generation"]),
            source_node=data["source_node"],
            replica_node=data["replica_node"],
            committed=bool(data["committed"]),
            payload_bytes=int(data["payload_bytes"]),
            commit_time_ms=int(data["commit_time_ms"]),
            replay_time_ms=int(data["replay_time_ms"]),
            delay_ms=int(data["delay_ms"]),
            tombstone_applied=bool(data["tombstone_applied"]),
            original_time=dict(data["original_time"]),
            original_delay=dict(data["original_delay"]),
            migration=data.get("migration"),
            error_upper_bound=Fraction(data["error_upper_bound_exact"]),
            error_sources=tuple(data["error_sources"]),
            location=data.get("location", ""),
            content_digest=data.get("content_digest", ""),
        )


@dataclass(frozen=True)
class LogRecord:
    seq: int
    tx_id: str
    tx_num: int
    payload_bytes: int
    applied: bool
    replay_time_ms: int | None
    location: str

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "tx_id": self.tx_id,
            "tx_num": self.tx_num,
            "payload_bytes": self.payload_bytes,
            "applied": self.applied,
            "replay_time_ms": self.replay_time_ms,
            "location": self.location,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "LogRecord":
        return cls(
            seq=int(data["seq"]),
            tx_id=data["tx_id"],
            tx_num=int(data["tx_num"]),
            payload_bytes=int(data["payload_bytes"]),
            applied=bool(data["applied"]),
            replay_time_ms=data.get("replay_time_ms"),
            location=data.get("location", ""),
        )


@dataclass
class SourceBundle:
    v1_records: list[PositionRecord] = field(default_factory=list)
    v2_records: list[PositionRecord] = field(default_factory=list)
    log_records: list[LogRecord] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    v1_digest: str = ""
    v2_digest: str = ""
    logs_digest: str = ""


def _fraction_json(value: Fraction) -> float | int:
    from .canonical import fraction_to_json

    return fraction_to_json(value)


def _fraction_text(value: Fraction) -> str:
    from .canonical import fraction_text

    return fraction_text(value)
