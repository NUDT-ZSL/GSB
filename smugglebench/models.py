"""Immutable-ish data contracts shared by normalization, parsing and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FlowRecord:
    connection_id: str
    direction: str
    seq: int
    logical_ms: int
    payload: bytes
    source_file: str
    record_index: int
    line_number: int
    arrival_index: int
    connection_hint: str | None = None

    def location(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "record_index": self.record_index,
            "line_number": self.line_number,
        }


@dataclass(frozen=True)
class Rejection:
    source_file: str
    record_index: int
    line_number: int
    code: str
    message: str
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_file": self.source_file,
            "record_index": self.record_index,
            "line_number": self.line_number,
            "code": self.code,
            "message": self.message,
        }
        if self.raw is not None:
            result["raw"] = self.raw
        return result


@dataclass(frozen=True)
class TransitionConstraint:
    from_states: tuple[str, ...]
    event: str | None
    conditions: dict[str, Any]
    action: str
    code: str
    index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_states": list(self.from_states),
            "event": self.event,
            "conditions": self.conditions,
            "action": self.action,
            "code": self.code,
            "index": self.index,
        }


@dataclass(frozen=True)
class Rule:
    canonical_name: str
    display_name: str
    generation: int
    effective_from_ms: int
    effective_to_ms: int | None
    enabled: bool
    tombstone: bool
    transitions: tuple[TransitionConstraint, ...]
    source_kind: str
    source_file: str
    record_index: int
    depends_on: tuple[str, ...] = ()
    raw_match: str | None = None

    def authority_key(self) -> tuple[Any, ...]:
        source_priority = 2 if self.source_kind == "v2" else 1
        return (
            self.generation,
            self.effective_from_ms,
            source_priority,
            self.canonical_name,
            self.source_file,
            self.record_index,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "display_name": self.display_name,
            "generation": self.generation,
            "effective_from_ms": self.effective_from_ms,
            "effective_to_ms": self.effective_to_ms,
            "enabled": self.enabled,
            "tombstone": self.tombstone,
            "transitions": [transition.to_dict() for transition in self.transitions],
            "source_kind": self.source_kind,
            "source_file": self.source_file,
            "record_index": self.record_index,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class StateTransition:
    state: str
    event: str
    next_state: str
    offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "event": self.event,
            "next_state": self.next_state,
            "offset": self.offset,
        }


@dataclass(frozen=True)
class ParsedMessage:
    connection_id: str
    direction: str
    request_index: int
    start_offset: int
    end_offset: int
    raw: bytes
    state_trace: tuple[StateTransition, ...]
    start_line: str
    method: str
    target: str
    version: str
    headers: tuple[tuple[str, str], ...]
    framing: str
    content_length: int | None
    transfer_encoding: tuple[str, ...]
    body: bytes
    chunk_sizes: tuple[int, ...]
    trailers: tuple[tuple[str, str], ...]
    next_boundary: int
    interpretation: dict[str, Any]
    anomalies: tuple[str, ...] = ()
    complete: bool = True
    flow_seq_start: int = 0
    flow_seq_end: int = 0

    def stable_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "direction": self.direction,
            "request_index": self.request_index,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "start_line": self.start_line,
            "headers": [list(header) for header in self.headers],
            "framing": self.framing,
            "content_length": self.content_length,
            "transfer_encoding": list(self.transfer_encoding),
            "body_sha256": _bytes_digest(self.body),
            "body_length": len(self.body),
            "chunk_sizes": list(self.chunk_sizes),
            "trailers": [list(trailer) for trailer in self.trailers],
            "next_boundary": self.next_boundary,
            "interpretation": self.interpretation,
            "anomalies": list(self.anomalies),
            "complete": self.complete,
            "state_trace": [transition.to_dict() for transition in self.state_trace],
        }


@dataclass(frozen=True)
class MutationSpec:
    mutation_id: str
    connection_id: str
    request_index: int
    target: str
    operation: str
    parameters: dict[str, Any]
    delay_ms: int
    source_file: str
    record_index: int
    seed: int = 0

    @property
    def stable_payload(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "connection_id": self.connection_id,
            "request_index": self.request_index,
            "target": self.target,
            "operation": self.operation,
            "parameters": self.parameters,
            "seed": self.seed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.stable_payload,
            "delay_ms": self.delay_ms,
            "source_file": self.source_file,
            "record_index": self.record_index,
        }


@dataclass(frozen=True)
class RuleHit:
    rule_key: str
    generation: int
    effective_from_ms: int
    state: str
    event: str
    byte_offset: int
    action: str
    code: str
    transition_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_key": self.rule_key,
            "generation": self.generation,
            "effective_from_ms": self.effective_from_ms,
            "state": self.state,
            "event": self.event,
            "byte_offset": self.byte_offset,
            "action": self.action,
            "code": self.code,
            "transition_index": self.transition_index,
        }


@dataclass
class ConnectionResult:
    connection_id: str
    messages: list[ParsedMessage] = field(default_factory=list)
    transitions: list[StateTransition] = field(default_factory=list)
    rule_hits: list[RuleHit] = field(default_factory=list)
    suppressed_hits: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    candidate_rule_evaluations: dict[str, list[str]] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "completed": self.completed,
            "message_count": len(self.messages),
            "messages": [message.stable_dict() for message in self.messages],
            "transition_count": len(self.transitions),
            "transitions": [transition.to_dict() for transition in self.transitions],
            "rule_hits": [hit.to_dict() for hit in self.rule_hits],
            "suppressed_hits": self.suppressed_hits,
            "duplicates": self.duplicates,
            "errors": self.errors,
            "candidate_rule_evaluations": self.candidate_rule_evaluations,
        }


@dataclass
class MutationResult:
    mutation_id: str
    connection_id: str
    stable_key: str
    status: str
    provisional_verdict: str
    final_verdict: str
    overturn_count: int
    discrepancies: tuple[str, ...]
    local_interpretation: dict[str, Any]
    remote_interpretation: dict[str, Any] | None
    rule_hits: list[RuleHit]
    suppressed_hits: list[dict[str, Any]]
    candidate_rules: tuple[str, ...]
    error: dict[str, Any] | None
    echo_sha256: str | None
    echo_bytes_seen: int
    submit_monotonic_ns: int
    visible_monotonic_ns: int
    latency_ms: float
    delay_profile_ms: int
    connection_sequence: int
    sent: bool
    peer_outcome: str

    def deterministic_dict(self) -> dict[str, Any]:
        if self.sent:
            delay_status = "OBSERVED"
        elif self.error and self.error.get("code") == "E_BUDGET":
            delay_status = "NOT_SENT_BUDGET"
        elif self.error and self.error.get("code") == "E_TIMEOUT":
            delay_status = "NOT_SENT_TIMEOUT"
        else:
            delay_status = "NOT_SENT"
        return {
            "mutation_id": self.mutation_id,
            "connection_id": self.connection_id,
            "stable_key": self.stable_key,
            "status": self.status,
            "provisional_verdict": self.provisional_verdict,
            "final_verdict": self.final_verdict,
            "overturn_count": self.overturn_count,
            "discrepancies": list(self.discrepancies),
            "local_interpretation": self.local_interpretation,
            "remote_interpretation": self.remote_interpretation,
            "rule_hits": [hit.to_dict() for hit in self.rule_hits],
            "suppressed_hits": self.suppressed_hits,
            "candidate_rules": list(self.candidate_rules),
            "error": self.error,
            "echo_sha256": self.echo_sha256,
            "echo_bytes_seen": self.echo_bytes_seen,
            "delay_observation": {
                "profile_ms": self.delay_profile_ms,
                "status": delay_status,
                "clock": "monotonic_ns",
                "sent": self.sent,
                "peer_outcome": self.peer_outcome,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.deterministic_dict()
        result.update(
            {
                "submit_monotonic_ns": self.submit_monotonic_ns,
                "visible_monotonic_ns": self.visible_monotonic_ns,
                "latency_ms": self.latency_ms,
                "sent": self.sent,
                "peer_outcome": self.peer_outcome,
                "delay_profile_ms": self.delay_profile_ms,
            }
        )
        return result


@dataclass(frozen=True)
class WorkspaceData:
    root: str
    observed_clock_ms: int
    flows: tuple[FlowRecord, ...]
    mutations: tuple[MutationSpec, ...]
    rules: tuple[Rule, ...]
    rejections: tuple[Rejection, ...]
    sample_seed: int
    source_files: dict[str, str]


def _bytes_digest(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
