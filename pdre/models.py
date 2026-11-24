"""Typed normalized domain records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class SourceRef:
    category: str
    record_seq: int
    source_name: str

    @property
    def source_ref(self) -> str:
        return f"{self.category}:{self.source_name}:{self.record_seq}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_category": self.category,
            "source_name": self.source_name,
            "record_seq": self.record_seq,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class FieldError:
    code: str
    field: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "field": self.field, "message": self.message}


class RecordRejected(Exception):
    def __init__(self, field_errors: list[FieldError]):
        super().__init__("record rejected")
        self.field_errors = field_errors


@dataclass(frozen=True)
class Rejection:
    code: str
    source_category: str
    record_seq: int
    source_name: str
    message: str
    field_errors: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "source_category": self.source_category,
            "source_name": self.source_name,
            "record_seq": self.record_seq,
            "message": self.message,
            "field_errors": list(self.field_errors),
        }


@dataclass(frozen=True)
class EventRecord:
    event_seq: int
    endpoint_id: str
    process_guid: str
    parent_process_guid: Optional[str]
    policy_key: str
    action_kind: str
    risk_level: int
    observed_ms: int
    payload_digest: str
    source: SourceRef

    @property
    def uid(self) -> str:
        return self.source.source_ref

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_seq": self.event_seq,
            "endpoint_id": self.endpoint_id,
            "process_guid": self.process_guid,
            "parent_process_guid": self.parent_process_guid,
            "policy_key": self.policy_key,
            "action_kind": self.action_kind,
            "risk_level": self.risk_level,
            "observed_ms": self.observed_ms,
            "payload_digest": self.payload_digest,
            "source": self.source.to_dict(),
        }


@dataclass(frozen=True)
class LineageRecord:
    process_guid: str
    parent_process_guid: Optional[str]
    session_id: str
    source: SourceRef
    endpoint_id: Optional[str] = None

    @property
    def uid(self) -> str:
        return self.source.source_ref

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_guid": self.process_guid,
            "parent_process_guid": self.parent_process_guid,
            "session_id": self.session_id,
            "endpoint_id": self.endpoint_id,
            "source": self.source.to_dict(),
        }


@dataclass(frozen=True)
class PolicyRecord:
    policy_key: str
    raw_policy_key: str
    generation: int
    format: str
    effective_start_ms: int
    effective_end_ms: Optional[int]
    tombstone: bool
    source: SourceRef
    action_code: Optional[str] = None
    match_digest: Optional[str] = None
    effective_start_sec: Optional[int] = None
    effective_end_sec: Optional[int] = None
    clock_ms: Optional[int] = None

    @property
    def uid(self) -> str:
        return self.source.source_ref

    @property
    def authority_key(self) -> tuple[int, int, int, str]:
        end = self.effective_end_ms if self.effective_end_ms is not None else 2**63 - 1
        return (
            self.generation,
            self.effective_start_ms,
            end,
            self.source.source_ref,
        )

    def basis_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rule_generation": self.generation,
            "format": self.format,
            "source": self.source.to_dict(),
            "effective_start_ms": self.effective_start_ms,
            "effective_end_ms": self.effective_end_ms,
            "tombstone": self.tombstone,
        }
        if not self.tombstone:
            result.update(
                {
                    "action_code": self.action_code,
                    "match_digest": self.match_digest,
                }
            )
        if self.format == "v1":
            result.update(
                {
                    "effective_start_sec": self.effective_start_sec,
                    "effective_end_sec": self.effective_end_sec,
                    "observed_clock_ms": self.clock_ms,
                }
            )
        return result


@dataclass
class ParsedInput:
    events: list[EventRecord] = field(default_factory=list)
    lineage: list[LineageRecord] = field(default_factory=list)
    policies: list[PolicyRecord] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    read_errors: list[dict[str, Any]] = field(default_factory=list)
