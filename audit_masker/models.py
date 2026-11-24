from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def event_sort_key(event_id: str, seq: int) -> Tuple[str, int]:
    return (event_id, seq)


@dataclass(frozen=True)
class Rejection:
    source_format: str
    source_ref: str
    record_index: int
    code: str
    message: str
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_format": self.source_format,
            "source_ref": self.source_ref,
            "record_index": self.record_index,
            "code": self.code,
            "message": self.message,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class Event:
    event_id: str
    tenant: str
    actor: str
    category: str
    ts_ms: int
    seq: int
    payload: Dict[str, str]
    idempotency_key: str
    sync_marker: Optional[str] = None
    source_ref: str = ""
    source_index: int = 0

    @property
    def sort_key(self) -> Tuple[str, int]:
        return event_sort_key(self.event_id, self.seq)

    @property
    def event_key(self) -> str:
        return f"{self.event_id}#{self.seq}"

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "event_id": self.event_id,
            "tenant": self.tenant,
            "actor": self.actor,
            "category": self.category,
            "ts_ms": self.ts_ms,
            "seq": self.seq,
            "payload": dict(self.payload),
            "idempotency_key": self.idempotency_key,
        }
        if self.sync_marker is not None:
            result["sync_marker"] = self.sync_marker
        return result


@dataclass(frozen=True)
class RuleRecord:
    """A normalized v1 or v2 rule record before authority resolution."""

    source_kind: str
    source_ref: str
    record_index: int
    source_format: str
    raw: str
    rule_name: str
    effective_ms: int
    generation: int
    tombstone: bool
    field_path: Optional[str] = None
    mask: Optional[str] = None
    threshold: Optional[int] = None
    patch_id: Optional[str] = None

    @property
    def content_key(self) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        return (self.field_path, self.mask, self.threshold)

    @property
    def authority_key(self) -> Tuple[int, int, str]:
        return (self.generation, self.effective_ms, self.source_format)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "record_index": self.record_index,
            "source_format": self.source_format,
            "rule_name": self.rule_name,
            "field_path": self.field_path,
            "mask": self.mask,
            "threshold": self.threshold,
            "effective_ms": self.effective_ms,
            "generation": self.generation,
            "tombstone": self.tombstone,
            "patch_id": self.patch_id,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class CategoryDefault:
    category: str
    fields: Tuple[str, ...]
    mask: str
    rule_name: str
    generation: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "fields": list(self.fields),
            "mask": self.mask,
            "rule_name": self.rule_name,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class LayoutPolicy:
    mask_methods: Tuple[str, ...]
    threshold_mode: str = "higher_wins"
    version: str = "audit-mask-layout-v1"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mask_methods": list(self.mask_methods),
            "threshold_mode": self.threshold_mode,
            "version": self.version,
        }


@dataclass
class RuleConflict:
    rule_name: str
    generation: int
    field_values: List[Dict[str, Any]]
    message: str = "same rule name and generation contain incompatible decisions"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "generation": self.generation,
            "message": self.message,
            "field_values": list(self.field_values),
        }


@dataclass
class UnifiedRule:
    rule_name: str
    state: str
    sources: List[RuleRecord]
    field_path: Optional[str] = None
    mask: Optional[str] = None
    threshold: Optional[int] = None
    generation: Optional[int] = None
    effective_ms: Optional[int] = None
    authority_source: Optional[RuleRecord] = None
    conflict: Optional[RuleConflict] = None

    @property
    def is_active(self) -> bool:
        return self.state == "active"

    def dependency_paths(self) -> List[str]:
        paths = {
            record.field_path
            for record in self.sources
            if record.field_path is not None
        }
        if self.field_path is not None:
            paths.add(self.field_path)
        return sorted(paths)

    def signature(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "field_path": self.field_path,
            "mask": self.mask,
            "threshold": self.threshold,
            "generation": self.generation,
            "effective_ms": self.effective_ms,
            "conflict": self.conflict.to_dict() if self.conflict else None,
            "dependency_paths": self.dependency_paths(),
        }

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "rule_name": self.rule_name,
            "state": self.state,
            "field_path": self.field_path,
            "mask": self.mask,
            "threshold": self.threshold,
            "generation": self.generation,
            "effective_ms": self.effective_ms,
            "authority_source": (
                {
                    "source_kind": self.authority_source.source_kind,
                    "source_ref": self.authority_source.source_ref,
                    "record_index": self.authority_source.record_index,
                    "source_format": self.authority_source.source_format,
                }
                if self.authority_source
                else None
            ),
            "sources": [source.to_dict() for source in self.sources],
            "dependency_paths": self.dependency_paths(),
        }
        if self.conflict:
            result["conflict"] = self.conflict.to_dict()
        return result


@dataclass
class UnifiedView:
    layout: LayoutPolicy
    defaults: Dict[str, CategoryDefault]
    rules_by_name: Dict[str, UnifiedRule]
    conflicts: List[RuleConflict]
    fingerprint: str

    @property
    def active_rules(self) -> List[UnifiedRule]:
        return sorted(
            (rule for rule in self.rules_by_name.values() if rule.is_active),
            key=lambda rule: (rule.rule_name, rule.field_path or ""),
        )

    def state(self) -> Dict[str, Any]:
        return {
            "layout_fingerprint": self.fingerprint,
            "layout": self.layout.to_dict(),
            "defaults": {
                name: item.to_dict()
                for name, item in sorted(self.defaults.items())
            },
            "rules": {
                name: self.rules_by_name[name].signature()
                for name in sorted(self.rules_by_name)
            },
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


@dataclass
class FieldDecision:
    field_name: str
    field_path: str
    masked_value: str
    decision: str
    basis: str
    winning_rule_name: str
    winning_generation: int
    winning_threshold: Optional[int]
    winning_mask: str
    matches: List[Dict[str, Any]] = field(default_factory=list)
    suppressed: List[Dict[str, Any]] = field(default_factory=list)
    business_codes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field_path": self.field_path,
            "masked_value": self.masked_value,
            "decision": self.decision,
            "basis": self.basis,
            "winning_rule_name": self.winning_rule_name,
            "winning_generation": self.winning_generation,
            "winning_threshold": self.winning_threshold,
            "winning_mask": self.winning_mask,
            "matches": list(self.matches),
            "suppressed": list(self.suppressed),
            "business_codes": list(self.business_codes),
        }

    def ledger_row(self) -> Dict[str, Any]:
        return {
            "field_path": self.field_path,
            "decision": self.decision,
            "masked_value": self.masked_value,
            "winning_rule_name": self.winning_rule_name,
            "winning_generation": self.winning_generation,
            "winning_threshold": self.winning_threshold,
            "basis": self.basis,
            "matches": list(self.matches),
            "suppressed": list(self.suppressed),
            "business_codes": list(self.business_codes),
        }


@dataclass
class EventResult:
    event: Event
    fields: Dict[str, FieldDecision]
    status: str
    business_codes: List[str]
    decision_watermark: int = 0

    @property
    def masked_payload(self) -> Dict[str, str]:
        return {
            name: self.fields[name].masked_value
            for name in sorted(self.fields)
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event.event_id,
            "seq": self.event.seq,
            **(
                {"sync_marker": self.event.sync_marker}
                if self.event.sync_marker is not None
                else {}
            ),
            "tenant": self.event.tenant,
            "actor": self.event.actor,
            "category": self.event.category,
            "ts_ms": self.event.ts_ms,
            "idempotency_key": self.event.idempotency_key,
            "status": self.status,
            "business_codes": list(self.business_codes),
            "decision_watermark": self.decision_watermark,
            "masked_payload": self.masked_payload,
            "fields": {
                name: decision.to_dict()
                for name, decision in sorted(self.fields.items())
            },
        }

    def canonical_decision(self) -> Dict[str, Any]:
        """Result content used for incremental/full equivalence checks."""
        return {
            "status": self.status,
            "business_codes": sorted(set(self.business_codes)),
            "masked_payload": self.masked_payload,
            "fields": {
                name: {
                    key: value
                    for key, value in decision.to_dict().items()
                    if key != "business_codes"
                }
                for name, decision in sorted(self.fields.items())
            },
        }
