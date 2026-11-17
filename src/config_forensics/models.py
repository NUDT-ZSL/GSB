"""Data models used by deterministic parsing and adjudication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .issues import Issue

MISSING = object()


@dataclass
class RawEvidence:
    file_id: str
    file_index: int
    record_index: int
    evidence_id: str
    aliases: list[str]
    host_id: str
    sequence: int
    path: str
    value: Any
    algorithm: str
    digest: str
    digest_bits: int | None
    previous_evidence_id: str | None
    hmac_key_id: str | None = None
    sync_marker: Any = None
    sender_observed_value: Any = None
    has_sender_observed_value: bool = False
    tampered: bool = False
    conflict: bool = False

    def signature(self) -> tuple[str, str]:
        return (self.algorithm, self.digest)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "file_id": self.file_id,
            "file_index": self.file_index,
            "record_index": self.record_index,
            "evidence_id": self.evidence_id,
            "aliases": list(self.aliases),
            "host_id": self.host_id,
            "sequence": self.sequence,
            "path": self.path,
            "value": self.value,
            "algorithm": self.algorithm,
            "digest": self.digest,
            "digest_bits": self.digest_bits,
            "previous_evidence_id": self.previous_evidence_id,
            "hmac_key_id": self.hmac_key_id,
            "sync_marker": self.sync_marker,
            "sender_observed_value": self.sender_observed_value,
            "has_sender_observed_value": self.has_sender_observed_value,
            "tampered": self.tampered,
            "conflict": self.conflict,
        }
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RawEvidence":
        return cls(
            file_id=value["file_id"],
            file_index=value["file_index"],
            record_index=value["record_index"],
            evidence_id=value["evidence_id"],
            aliases=list(value.get("aliases") or []),
            host_id=value["host_id"],
            sequence=value["sequence"],
            path=value["path"],
            value=value.get("value"),
            algorithm=value["algorithm"],
            digest=value["digest"],
            digest_bits=value.get("digest_bits"),
            previous_evidence_id=value.get("previous_evidence_id"),
            hmac_key_id=value.get("hmac_key_id"),
            sync_marker=value.get("sync_marker"),
            sender_observed_value=value.get("sender_observed_value"),
            has_sender_observed_value=bool(value.get("has_sender_observed_value", False)),
            tampered=bool(value.get("tampered", False)),
            conflict=bool(value.get("conflict", False)),
        )


@dataclass
class FileResult:
    source_id: str
    source_name: str
    file_index: int
    file_id: str
    sha256: str
    status: str
    records: list[RawEvidence] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    payload_record_count: int = 0

    @property
    def parsed_count(self) -> int:
        return len(self.records)

    @property
    def integrity_failure_count(self) -> int:
        return sum(1 for record in self.records if record.tampered)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "file_index": self.file_index,
            "file_id": self.file_id,
            "sha256": self.sha256,
            "status": self.status,
            "record_count": self.payload_record_count,
            "parsed_count": self.parsed_count,
            "integrity_failure_count": self.integrity_failure_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def to_checkpoint_dict(self) -> dict[str, Any]:
        result = self.to_dict()
        result["records"] = [record.to_dict() for record in self.records]
        return result

    @classmethod
    def from_checkpoint_dict(cls, value: dict[str, Any]) -> "FileResult":
        return cls(
            source_id=value["source_id"],
            source_name=value["source_name"],
            file_index=value["file_index"],
            file_id=value["file_id"],
            sha256=value["sha256"],
            status=value["status"],
            records=[RawEvidence.from_dict(item) for item in value.get("records", [])],
            issues=[Issue.from_dict(item) for item in value.get("issues", [])],
            payload_record_count=int(value.get("record_count", 0)),
        )


@dataclass(frozen=True)
class BaselineRange:
    minimum: Any = MISSING
    maximum: Any = MISSING
    minimum_inclusive: bool = True
    maximum_inclusive: bool = True
    allowed_values: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "min_inclusive": self.minimum_inclusive,
            "max_inclusive": self.maximum_inclusive,
        }
        if self.minimum is not MISSING:
            result["min"] = self.minimum
        if self.maximum is not MISSING:
            result["max"] = self.maximum
        if self.allowed_values:
            result["allowed_values"] = list(self.allowed_values)
        return result


@dataclass(frozen=True)
class BaselineRule:
    host_id: str
    path: str
    source: str
    effective_from_seq: int | None
    effective_to_seq: int | None
    effective_from_inclusive: bool
    effective_to_inclusive: bool
    ranges: tuple[BaselineRange, ...]
    declaration_index: int

    def active_at(self, sequence: int) -> bool:
        if self.effective_from_seq is not None:
            if self.effective_from_inclusive:
                if sequence < self.effective_from_seq:
                    return False
            elif sequence <= self.effective_from_seq:
                return False
        if self.effective_to_seq is not None:
            if self.effective_to_inclusive:
                if sequence > self.effective_to_seq:
                    return False
            elif sequence >= self.effective_to_seq:
                return False
        return True

    def permitted(self, value: Any) -> bool:
        for value_range in self.ranges:
            if _range_permits(value_range, value):
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "path": self.path,
            "source": self.source,
            "effective_from_seq": self.effective_from_seq,
            "effective_to_seq": self.effective_to_seq,
            "effective_from_inclusive": self.effective_from_inclusive,
            "effective_to_inclusive": self.effective_to_inclusive,
            "ranges": [item.to_dict() for item in self.ranges],
            "declaration_index": self.declaration_index,
        }


def _same_json_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _range_permits(value_range: BaselineRange, value: Any) -> bool:
    if value_range.allowed_values:
        if any(_same_json_value(value, allowed) for allowed in value_range.allowed_values):
            return True
    lower = value_range.minimum
    upper = value_range.maximum
    if lower is MISSING and upper is MISSING:
        return False

    numeric = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (lower is MISSING or (isinstance(lower, (int, float)) and not isinstance(lower, bool)))
        and (upper is MISSING or (isinstance(upper, (int, float)) and not isinstance(upper, bool)))
    )
    textual = (
        isinstance(value, str)
        and (lower is MISSING or isinstance(lower, str))
        and (upper is MISSING or isinstance(upper, str))
    )
    if not numeric and not textual:
        return False
    try:
        if lower is not MISSING:
            if value_range.minimum_inclusive:
                if value < lower:
                    return False
            elif value <= lower:
                return False
        if upper is not MISSING:
            if value_range.maximum_inclusive:
                if value > upper:
                    return False
            elif value >= upper:
                return False
    except TypeError:
        return False
    return True
