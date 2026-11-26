from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .canonical import canonical_json, content_hash


@dataclass(frozen=True)
class Candidate:
    slot_key: str
    value: Any
    confidence: float
    source_turn: int
    candidate_key: str

    def core_dict(self) -> dict[str, Any]:
        return {
            "slot_key": self.slot_key,
            "value": self.value,
            "confidence": self.confidence,
            "source_turn": self.source_turn,
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.core_dict()
        data["candidate_key"] = self.candidate_key
        return data

    @property
    def value_key(self) -> str:
        return canonical_json(self.value)

    @classmethod
    def build(
        cls,
        slot_key: str,
        value: Any,
        confidence: float,
        source_turn: int,
        candidate_key: str | None = None,
    ) -> "Candidate":
        key = candidate_cache_key(slot_key, value, confidence, source_turn)
        if candidate_key is not None and candidate_key != key:
            raise ValueError(f"candidate_key mismatch: expected {key}, received {candidate_key}")
        return cls(
            slot_key=slot_key,
            value=value,
            confidence=confidence,
            source_turn=source_turn,
            candidate_key=key,
        )


def candidate_cache_key(
    slot_key: str, value: Any, confidence: float, source_turn: int
) -> str:
    return content_hash(
        {
            "node": "candidate",
            "candidate": {
                "slot_key": slot_key,
                "value": value,
                "confidence": confidence,
                "source_turn": source_turn,
            },
        }
    )


@dataclass(frozen=True)
class SlotState:
    winner: Candidate
    overwritten: tuple[Candidate, ...]
    conflict: bool
    updated_at_ms: int

    def to_semantic_dict(self) -> dict[str, Any]:
        return {
            "winner": self.winner.to_dict(),
            "overwritten": [item.to_dict() for item in self.overwritten],
            "conflict": self.conflict,
            "updated_at_ms": self.updated_at_ms,
        }


@dataclass(frozen=True)
class InternalState:
    session_id: str
    state_generation: int
    updated_at_ms: int
    slots: tuple[tuple[str, SlotState], ...]
    pending_clarifications: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        state_generation: int,
        updated_at_ms: int,
        slots: dict[str, SlotState] | Iterable[tuple[str, SlotState]],
        pending_clarifications: Iterable[str],
    ) -> "InternalState":
        slot_items = slots.items() if isinstance(slots, dict) else slots
        return cls(
            session_id=session_id,
            state_generation=state_generation,
            updated_at_ms=updated_at_ms,
            slots=tuple(sorted(slot_items, key=lambda item: item[0])),
            pending_clarifications=tuple(sorted(set(pending_clarifications))),
        )

    @property
    def slot_map(self) -> dict[str, SlotState]:
        return dict(self.slots)

    @property
    def state_checksum(self) -> str:
        return content_hash(self.to_semantic_dict())

    def to_semantic_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state_generation": self.state_generation,
            "updated_at_ms": self.updated_at_ms,
            "slots": {key: value.to_semantic_dict() for key, value in self.slots},
            "pending_clarifications": list(self.pending_clarifications),
        }

    def to_view(self) -> dict[str, Any]:
        data = self.to_semantic_dict()
        data["state_checksum"] = self.state_checksum
        return data


@dataclass(frozen=True)
class ReadResult:
    state: InternalState
    audit_history: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    completions: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    checksum_source: str = "computed:state_checksum"
    file_checksum: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "internal_state": self.state.to_view(),
            "audit_history": list(self.audit_history),
            "completions": list(self.completions),
            "checksum_source": self.checksum_source,
            "file_checksum": self.file_checksum,
        }


def completion(
    field: str,
    source: str,
    value: Any,
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "field": field,
        "source": source,
        "value": value,
        "reason": reason,
    }

