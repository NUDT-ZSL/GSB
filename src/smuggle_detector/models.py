"""Immutable normalized input and output models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MessageRecord:
    connection_id: str
    direction: str
    seq: int
    ts_ms: int
    payload: bytes
    source_category: str
    record_index: int

    def to_doc(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "direction": self.direction,
            "seq": self.seq,
            "ts_ms": self.ts_ms,
            "payload_hex": self.payload.hex(),
            "source_category": self.source_category,
            "record_index": self.record_index,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "MessageRecord":
        return cls(
            connection_id=doc["connection_id"],
            direction=doc["direction"],
            seq=doc["seq"],
            ts_ms=doc["ts_ms"],
            payload=bytes.fromhex(doc["payload_hex"]),
            source_category=doc.get("source_category", "messages"),
            record_index=doc.get("record_index", 0),
        )


@dataclass(frozen=True)
class RuleRecord:
    canonical_name: str
    generation: int
    effective_ms: int
    direction: str
    patterns: tuple[str, ...]
    tombstone: bool
    enabled: bool
    source_category: str
    record_index: int

    def semantic_doc(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "generation": self.generation,
            "effective_ms": self.effective_ms,
            "direction": self.direction,
            "patterns": list(self.patterns),
            "tombstone": self.tombstone,
            "enabled": self.enabled,
        }

    def to_doc(self) -> dict[str, Any]:
        doc = self.semantic_doc()
        doc["source_category"] = self.source_category
        doc["record_index"] = self.record_index
        return doc

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "RuleRecord":
        return cls(
            canonical_name=doc["canonical_name"],
            generation=doc["generation"],
            effective_ms=doc["effective_ms"],
            direction=doc["direction"],
            patterns=tuple(doc["patterns"]),
            tombstone=doc["tombstone"],
            enabled=doc["enabled"],
            source_category=doc.get("source_category", "rules"),
            record_index=doc.get("record_index", 0),
        )


@dataclass(frozen=True)
class Rejection:
    source_category: str
    record_index: int
    code: str
    detail: str

    def to_doc(self) -> dict[str, Any]:
        return {
            "source_category": self.source_category,
            "record_index": self.record_index,
            "code": self.code,
            "detail": self.detail,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Rejection":
        return cls(
            source_category=doc["source_category"],
            record_index=doc["record_index"],
            code=doc["code"],
            detail=doc["detail"],
        )


@dataclass(frozen=True)
class Plan:
    concurrency: int
    connection_timeout_ms: int
    batch_budget_ms: int
    queue_limit: int
    output_path: str

    def to_doc(self) -> dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "connection_timeout_ms": self.connection_timeout_ms,
            "batch_budget_ms": self.batch_budget_ms,
            "queue_limit": self.queue_limit,
            "output_path": self.output_path,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Plan":
        return cls(
            concurrency=doc["concurrency"],
            connection_timeout_ms=doc["connection_timeout_ms"],
            batch_budget_ms=doc["batch_budget_ms"],
            queue_limit=doc["queue_limit"],
            output_path=doc["output_path"],
        )


@dataclass
class SampleInput:
    messages: list[MessageRecord] = field(default_factory=list)
    rules: list[RuleRecord] = field(default_factory=list)
    plan: Plan | None = None
    rejections: list[Rejection] = field(default_factory=list)
    source_files: dict[str, str] = field(default_factory=dict)

    def to_doc(self) -> dict[str, Any]:
        assert self.plan is not None
        return {
            "messages": [item.to_doc() for item in self.messages],
            "rules": [item.to_doc() for item in self.rules],
            "plan": self.plan.to_doc(),
            "rejections": [item.to_doc() for item in self.rejections],
            "source_files": dict(sorted(self.source_files.items())),
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "SampleInput":
        return cls(
            messages=[MessageRecord.from_doc(item) for item in doc["messages"]],
            rules=[RuleRecord.from_doc(item) for item in doc["rules"]],
            plan=Plan.from_doc(doc["plan"]),
            rejections=[Rejection.from_doc(item) for item in doc["rejections"]],
            source_files=dict(doc.get("source_files", {})),
        )

