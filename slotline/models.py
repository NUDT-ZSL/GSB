from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .canonical import canonical_json_bytes
from .errors import BusinessError, E_INVALID_SESSION


def _require_non_empty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(
            E_INVALID_SESSION,
            f"{field_name} must be a non-empty string",
            node="session",
        )
    return value


@dataclass(frozen=True)
class Candidate:
    key: str
    value: str
    source_turn: int
    confidence: float
    extractor_version: str

    def __post_init__(self) -> None:
        _require_non_empty(self.key, "candidate.key")
        _require_non_empty(self.value, "candidate.value")
        _require_non_empty(self.extractor_version, "candidate.extractor_version")
        if (
            not isinstance(self.source_turn, int)
            or isinstance(self.source_turn, bool)
            or self.source_turn < 1
        ):
            raise BusinessError(
                E_INVALID_SESSION,
                "candidate.source_turn must be a positive integer",
                node="session",
                slot=self.key,
            )
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not 0 <= float(self.confidence) <= 1
        ):
            raise BusinessError(
                E_INVALID_SESSION,
                "candidate.confidence must be within [0, 1]",
                node="session",
                slot=self.key,
            )
        object.__setattr__(self, "confidence", float(self.confidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source_turn": self.source_turn,
            "confidence": self.confidence,
            "extractor_version": self.extractor_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Candidate":
        return cls(
            key=value["key"],
            value=value["value"],
            source_turn=value["source_turn"],
            confidence=value["confidence"],
            extractor_version=value["extractor_version"],
        )


@dataclass(frozen=True)
class ClarificationRequest:
    slot: str
    reason: str = "low_confidence"

    def __post_init__(self) -> None:
        _require_non_empty(self.slot, "clarification.slot")
        _require_non_empty(self.reason, "clarification.reason")

    def to_dict(self) -> dict[str, str]:
        return {"slot": self.slot, "reason": self.reason}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClarificationRequest":
        return cls(slot=value["slot"], reason=value.get("reason", "low_confidence"))


@dataclass(frozen=True)
class Turn:
    turn_id: str
    index: int
    role: str
    text: str
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)
    clarifications: tuple[ClarificationRequest, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_empty(self.turn_id, "turn.turn_id")
        _require_non_empty(self.role, "turn.role")
        if self.role not in {"user", "assistant"}:
            raise BusinessError(
                E_INVALID_SESSION,
                "turn.role must be user or assistant",
                node=self.turn_id,
            )
        if not isinstance(self.index, int) or isinstance(self.index, bool) or self.index < 1:
            raise BusinessError(
                E_INVALID_SESSION,
                "turn.index must be a positive integer",
                node=self.turn_id,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "index": self.index,
            "role": self.role,
            "text": self.text,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "clarifications": [
                request.to_dict() for request in self.clarifications
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Turn":
        return cls(
            turn_id=value["turn_id"],
            index=value["index"],
            role=value["role"],
            text=value.get("text", ""),
            candidates=tuple(
                Candidate.from_dict(candidate)
                for candidate in value.get("candidates", [])
            ),
            clarifications=tuple(
                ClarificationRequest.from_dict(request)
                for request in value.get("clarifications", [])
            ),
        )


@dataclass(frozen=True)
class Session:
    session_id: str
    seed: int
    generator_version: str
    turns: tuple[Turn, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.session_id, "session.session_id")
        _require_non_empty(self.generator_version, "session.generator_version")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise BusinessError(
                E_INVALID_SESSION,
                "session.seed must be an integer",
                node="session",
            )
        seen_ids: set[str] = set()
        seen_indexes: set[int] = set()
        for turn in self.turns:
            if turn.turn_id in seen_ids:
                raise BusinessError(
                    E_INVALID_SESSION,
                    f"duplicate turn_id: {turn.turn_id}",
                    node=turn.turn_id,
                )
            if turn.index in seen_indexes:
                raise BusinessError(
                    E_INVALID_SESSION,
                    f"duplicate turn index: {turn.index}",
                    node=turn.turn_id,
                )
            seen_ids.add(turn.turn_id)
            seen_indexes.add(turn.index)
            if turn.role == "user":
                for candidate in turn.candidates:
                    if candidate.source_turn > turn.index:
                        raise BusinessError(
                            E_INVALID_SESSION,
                            "candidate.source_turn cannot be in the future",
                            node=turn.turn_id,
                            slot=candidate.key,
                        )
            elif turn.candidates:
                raise BusinessError(
                    E_INVALID_SESSION,
                    "assistant turns may not own candidates",
                    node=turn.turn_id,
                )
            if turn.role == "user" and turn.clarifications:
                raise BusinessError(
                    E_INVALID_SESSION,
                    "user turns may not contain clarification requests",
                    node=turn.turn_id,
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seed": self.seed,
            "generator_version": self.generator_version,
            "metadata": self.metadata,
            "turns": [turn.to_dict() for turn in self.turns],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Session":
        return cls(
            session_id=value["session_id"],
            seed=value["seed"],
            generator_version=value["generator_version"],
            metadata=dict(value.get("metadata", {})),
            turns=tuple(Turn.from_dict(turn) for turn in value.get("turns", [])),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def ordered_turns(self) -> list[Turn]:
        return sorted(self.turns, key=lambda turn: turn.index)

    def get_turn(self, turn_id: str) -> Turn:
        for turn in self.turns:
            if turn.turn_id == turn_id:
                return turn
        raise BusinessError(
            E_INVALID_SESSION,
            f"unknown turn: {turn_id}",
            node=turn_id,
        )

    def without_turn_ids(self, turn_ids: Iterable[str]) -> "Session":
        removed = set(turn_ids)
        return Session(
            session_id=self.session_id,
            seed=self.seed,
            generator_version=self.generator_version,
            turns=tuple(turn for turn in self.turns if turn.turn_id not in removed),
            metadata=dict(self.metadata),
        )

    def append_turn(self, turn: Turn) -> "Session":
        if any(existing.turn_id == turn.turn_id for existing in self.turns):
            raise BusinessError(
                E_INVALID_SESSION,
                f"duplicate turn_id: {turn.turn_id}",
                node=turn.turn_id,
            )
        maximum = max((existing.index for existing in self.turns), default=0)
        if turn.index != maximum + 1:
            raise BusinessError(
                E_INVALID_SESSION,
                "appended turns must be at the tail and use index max(index)+1",
                node=turn.turn_id,
            )
        return Session(
            session_id=self.session_id,
            seed=self.seed,
            generator_version=self.generator_version,
            turns=tuple(sorted((*self.turns, turn), key=lambda item: item.index)),
            metadata=dict(self.metadata),
        )

    def max_index(self) -> int:
        return max((turn.index for turn in self.turns), default=0)
