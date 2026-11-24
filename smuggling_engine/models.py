"""Typed data structures shared by the parser, ruleset, engine and checkpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _stable_value(val)
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


@dataclass(frozen=True)
class Rejection:
    source: str
    record_index: int
    code: str
    message: str
    record: dict[str, Any] = field(default_factory=dict)
    record_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "source": self.source,
            "record_index": self.record_index,
            "code": self.code,
            "message": self.message,
            "record": _stable_value(self.record),
        }
        if self.record_ref is not None:
            result["record_ref"] = self.record_ref
        return result


@dataclass
class RawMessage:
    connection_id: str
    direction: str
    sequence: int
    timestamp_ms: int
    payload: bytes
    source_index: int
    arrival_index: int
    sync_marker: str | None = None
    duplicate_count: int = 0
    duplicate_conflicts: int = 0
    duplicate_bytes: int = 0

    @property
    def payload_hex(self) -> str:
        return self.payload.hex()

    def evidence_dict(self) -> dict[str, Any]:
        return {
            "source_index": self.source_index,
            "arrival_index": self.arrival_index,
            "connection_id": self.connection_id,
            "direction": self.direction,
            "sequence": self.sequence,
            "timestamp_ms": self.timestamp_ms,
            "payload_hex": self.payload_hex,
            "payload_sha256": __import__("hashlib").sha256(self.payload).hexdigest(),
            "payload_length": len(self.payload),
            "duplicate_count": self.duplicate_count,
            "duplicate_conflicts": self.duplicate_conflicts,
            "duplicate_bytes": self.duplicate_bytes,
            "sync_marker": self.sync_marker,
        }


@dataclass(frozen=True)
class RuleRecord:
    canonical_name: str
    effective_at_ms: int
    generation: int
    direction: str
    tombstone: bool
    depends_on: tuple[str, ...]
    pattern: str | None
    source: str
    source_index: int
    source_ref: str
    enabled: bool = True
    raw_name: str | None = None

    @property
    def active_candidate(self) -> bool:
        return self.enabled and not self.tombstone

    def precedence(self) -> tuple[Any, ...]:
        # Higher generation and later effective time win.  A tombstone wins an
        # otherwise exact tie, and v2 metadata outranks v1.
        return (
            self.generation,
            self.effective_at_ms,
            1 if self.tombstone else 0,
            1 if self.source == "rules_v2" else 0,
            -self.source_index,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "effective_at_ms": self.effective_at_ms,
            "generation": self.generation,
            "direction": self.direction,
            "tombstone": self.tombstone,
            "depends_on": list(self.depends_on),
            "pattern": self.pattern,
            "source": self.source,
            "source_index": self.source_index,
            "source_ref": self.source_ref,
            "enabled": self.enabled,
            "raw_name": self.raw_name,
        }


@dataclass(frozen=True)
class RuleEntity:
    canonical_name: str
    winner: RuleRecord
    history: tuple[RuleRecord, ...]

    @property
    def active(self) -> bool:
        return self.winner.active_candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "active": self.active,
            "winner": self.winner.to_dict(),
            "history": [record.to_dict() for record in self.history],
        }


@dataclass(frozen=True)
class RuleView:
    entities: dict[str, RuleEntity]
    records: tuple[RuleRecord, ...]
    version: str
    generation: int
    observed_clock_ms: int
    rejects: tuple[Rejection, ...] = ()
    previous_version: str | None = None
    merge_policy_version: str = "rules-merge-v1"
    changed_dimensions: tuple[str, ...] = ()

    @property
    def replaced(self) -> bool:
        return self.previous_version is None or self.previous_version != self.version

    @property
    def active_rules(self) -> tuple[RuleRecord, ...]:
        return tuple(
            entity.winner
            for _, entity in sorted(self.entities.items())
            if entity.active
        )

    @property
    def tombstoned_rules(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, entity in sorted(self.entities.items())
            if entity.winner.tombstone or not entity.winner.enabled
        )

    def candidates_for_match(self) -> tuple[RuleRecord, ...]:
        """Return every historical record with a pattern.

        Historical records are needed to identify connections affected by a
        tombstone even though only the winning entity can produce a final
        decision.
        """

        result = [
            record
            for record in self.records
            if record.pattern is not None and record.enabled
        ]
        return tuple(
            sorted(
                result,
                key=lambda record: (
                    record.canonical_name,
                    -record.generation,
                    -record.effective_at_ms,
                    record.source,
                    record.source_index,
                ),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generation": self.generation,
            "observed_clock_ms": self.observed_clock_ms,
            "merge_policy_version": self.merge_policy_version,
            "replaced": self.replaced,
            "previous_version": self.previous_version,
            "changed_dimensions": list(self.changed_dimensions),
            "entities": [entity.to_dict() for _, entity in sorted(self.entities.items())],
        }


@dataclass(frozen=True)
class Finding:
    code: str
    connection_id: str
    message_ordinal: int
    byte_start: int
    byte_end: int
    byte_offset: int
    details: dict[str, Any]
    match_tokens: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "connection_id": self.connection_id,
            "message_ordinal": self.message_ordinal,
            "byte_offset": self.byte_offset,
            "byte_range": {"start": self.byte_start, "end": self.byte_end},
            "details": _stable_value(self.details),
            "match_tokens": list(self.match_tokens),
        }


@dataclass(frozen=True)
class RuleHit:
    canonical_name: str
    generation: int
    effective_at_ms: int
    direction: str
    source: str
    source_index: int
    pattern: str
    finding_code: str
    byte_start: int
    byte_end: int
    byte_offset: int
    record_active: bool
    record_valid: bool
    selected: bool

    def precedence(self) -> tuple[Any, ...]:
        return (
            -self.generation,
            -self.effective_at_ms,
            self.canonical_name,
            self.finding_code,
            self.byte_offset,
            self.source,
            self.source_index,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "generation": self.generation,
            "effective_at_ms": self.effective_at_ms,
            "direction": self.direction,
            "source": self.source,
            "source_index": self.source_index,
            "pattern": self.pattern,
            "finding_code": self.finding_code,
            "byte_range": {"start": self.byte_start, "end": self.byte_end},
            "byte_offset": self.byte_offset,
            "record_active": self.record_active,
            "record_valid": self.record_valid,
            "selected": self.selected,
        }


@dataclass
class ConnectionReport:
    connection_id: str
    status: str
    verdict: str
    business_code: str | None
    errors: list[dict[str, Any]]
    streams: dict[str, Any]
    findings: list[Finding]
    hits: list[RuleHit]
    selected_rule: str | None
    selected_generation: int | None
    selected_finding_code: str | None
    invalidated_hits: tuple[str, ...] = ()
    affected_by_invalid_rules: bool = False
    terminated: bool = False
    termination_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "connection_id": self.connection_id,
            "status": self.status,
            "verdict": self.verdict,
            "business_code": self.business_code,
            "errors": _stable_value(self.errors),
            "streams": _stable_value(self.streams),
            "findings": [finding.to_dict() for finding in self.findings],
            "hits": [hit.to_dict() for hit in self.hits],
            "selected_rule": self.selected_rule,
            "selected_generation": self.selected_generation,
            "selected_finding_code": self.selected_finding_code,
            "invalidated_hits": list(self.invalidated_hits),
            "affected_by_invalid_rules": self.affected_by_invalid_rules,
            "terminated": self.terminated,
            "termination_reason": self.termination_reason,
        }
        result["evidence_chain"] = self.evidence_chain()
        return result

    def evidence_chain(self) -> list[dict[str, Any]]:
        request = self.streams.get("directions", {}).get("request", {})
        fragments = request.get("fragments", [])
        chain = []
        for finding in self.findings:
            overlapping = []
            for fragment in fragments:
                fragment_range = fragment.get("byte_range", {})
                start = int(fragment_range.get("start", 0))
                end = int(fragment_range.get("end", 0))
                if start < finding.byte_end and end > finding.byte_start:
                    overlapping.append(
                        {
                            "sequence": fragment.get("sequence"),
                            "source_index": fragment.get("source_index"),
                            "payload_hex": fragment.get("payload_hex"),
                            "payload_sha256": fragment.get("payload_sha256"),
                            "byte_range": {"start": start, "end": end},
                        }
                    )
            chain.append(
                {
                    "business_code": finding.code,
                    "byte_range": {
                        "start": finding.byte_start,
                        "end": finding.byte_end,
                    },
                    "source_fragments": overlapping,
                    "rule_hits": [
                        {
                            "canonical_name": hit.canonical_name,
                            "generation": hit.generation,
                            "selected": hit.selected,
                            "valid": hit.record_valid,
                            "byte_range": {
                                "start": hit.byte_start,
                                "end": hit.byte_end,
                            },
                        }
                        for hit in self.hits
                        if hit.finding_code == finding.code
                        and hit.byte_offset == finding.byte_offset
                    ],
                }
            )
        return chain

    def stable_decision_dict(self) -> dict[str, Any]:
        """Deterministic decision material used for incremental comparisons."""

        return {
            "connection_id": self.connection_id,
            "status": self.status,
            "verdict": self.verdict,
            "business_code": self.business_code,
            "errors": _stable_value(self.errors),
            "findings": [finding.to_dict() for finding in self.findings],
            "selected_rule": self.selected_rule,
            "selected_generation": self.selected_generation,
            "selected_finding_code": self.selected_finding_code,
            "terminated": self.terminated,
            "termination_reason": self.termination_reason,
        }


@dataclass
class EngineReport:
    ruleset: dict[str, Any]
    closure: dict[str, Any]
    connections: list[ConnectionReport]
    rejected: list[Rejection]
    terminated_connections: list[str]
    summary: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        summary = dict(self.summary)
        if not summary:
            summary = self._build_summary()
        return {
            "schema_version": self.schema_version,
            "metadata": _stable_value(self.metadata),
            "ruleset": _stable_value(self.ruleset),
            "closure": _stable_value(self.closure),
            "summary": _stable_value(summary),
            "connections": [item.to_dict() for item in self.connections],
            "terminated_connections": list(self.terminated_connections),
            "rejected": [item.to_dict() for item in self.rejected],
        }

    def _build_summary(self) -> dict[str, Any]:
        verdicts: dict[str, int] = {}
        for connection in self.connections:
            verdicts[connection.verdict] = verdicts.get(connection.verdict, 0) + 1
        return {
            "connection_count": len(self.connections),
            "terminated_count": len(self.terminated_connections),
            "rejected_count": len(self.rejected),
            "verdict_counts": dict(sorted(verdicts.items())),
        }

    def stable_decision_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ruleset_version": self.ruleset.get("version"),
            "closure": _stable_value(self.closure),
            "connections": [
                item.stable_decision_dict()
                for item in sorted(self.connections, key=lambda item: item.connection_id)
            ],
            "rejected": [item.to_dict() for item in self.rejected],
        }

    def deterministic_dict(self) -> dict[str, Any]:
        """Full report payload without non-deterministic execution metadata."""

        result = self.to_dict()
        result.pop("metadata", None)
        return result

