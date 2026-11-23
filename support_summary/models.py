"""Typed domain models and JSON parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .canonical import sha256_value


@dataclass(frozen=True)
class Turn:
    turn_index: int
    role: str
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "role": self.role,
            "text": self.text,
        }


@dataclass(frozen=True)
class SummaryUnit:
    unit_id: str
    turn_index: int
    unit_index: int
    text: str
    truth: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "turn_index": self.turn_index,
            "unit_index": self.unit_index,
            "text": self.text,
            "truth": self.truth,
        }


@dataclass(frozen=True)
class RewriteConstraints:
    forbidden_words: Tuple[str, ...] = ()
    preserve_terms: Tuple[str, ...] = ()
    max_chars: int = 120

    def to_dict(self) -> Dict[str, Any]:
        return {
            "forbidden_words": list(self.forbidden_words),
            "preserve_terms": list(self.preserve_terms),
            "max_chars": self.max_chars,
        }


@dataclass(frozen=True)
class Session:
    session_id: str
    content_version: int
    turns: Tuple[Turn, ...]
    units: Tuple[SummaryUnit, ...]
    constraints: RewriteConstraints

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "content_version": self.content_version,
            "turns": [turn.to_dict() for turn in self.turns],
            "units": [unit.to_dict() for unit in self.units],
            "rewrite_constraints": self.constraints.to_dict(),
        }

    @property
    def content_hash(self) -> str:
        return sha256_value(self.to_dict())

    def to_append_dict(
        self, extra_turns: Sequence[Turn], extra_units: Sequence[SummaryUnit]
    ) -> Dict[str, Any]:
        value = self.to_dict()
        value["content_version"] = self.content_version + 1
        value["turns"].extend(turn.to_dict() for turn in extra_turns)
        value["units"].extend(unit.to_dict() for unit in extra_units)
        return value


@dataclass(frozen=True)
class SummaryConfig:
    config_version: int
    precision: str
    block_size: int
    weights: Mapping[str, float]
    rewrite_strength: float
    smoothing: float
    max_sessions_per_stage: int
    max_resident_frequency: int
    max_intermediate_memory_bytes: int
    resource_policy: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config_version": self.config_version,
            "precision": self.precision,
            "block_size": self.block_size,
            "weights": dict(self.weights),
            "rewrite_strength": self.rewrite_strength,
            "smoothing": self.smoothing,
            "limits": {
                "max_sessions_per_stage": self.max_sessions_per_stage,
                "max_resident_frequency": self.max_resident_frequency,
                "max_intermediate_memory_bytes": self.max_intermediate_memory_bytes,
            },
            "resource_policy": self.resource_policy,
        }

    @property
    def content_hash(self) -> str:
        return sha256_value(self.to_dict())


@dataclass(frozen=True)
class Vocabulary:
    vocab_version: str
    words: Tuple[str, ...]

    @property
    def word_set(self) -> frozenset[str]:
        return frozenset(self.words)

    @property
    def content_hash(self) -> str:
        return sha256_value(
            {"vocab_version": self.vocab_version, "words": list(self.words)}
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"vocab_version": self.vocab_version, "words": list(self.words)}


STAGES: Tuple[str, ...] = (
    "chunk_tokenize",
    "unit_score",
    "rewrite",
    "summary_merge",
)

WEIGHT_NAMES: Tuple[str, ...] = (
    "position",
    "length",
    "frequency",
    "coverage",
)


def session_from_dict(value: Mapping[str, Any]) -> Session:
    constraints_value = value.get("rewrite_constraints", {})
    return Session(
        session_id=str(value["session_id"]),
        content_version=int(value.get("content_version", 1)),
        turns=tuple(
            Turn(
                turn_index=int(turn["turn_index"]),
                role=str(turn["role"]),
                text=str(turn["text"]),
            )
            for turn in value.get("turns", [])
        ),
        units=tuple(
            SummaryUnit(
                unit_id=str(unit["unit_id"]),
                turn_index=int(unit["turn_index"]),
                unit_index=int(unit["unit_index"]),
                text=str(unit["text"]),
                truth=bool(unit.get("truth", True)),
            )
            for unit in value.get("units", [])
        ),
        constraints=RewriteConstraints(
            forbidden_words=tuple(constraints_value.get("forbidden_words", [])),
            preserve_terms=tuple(constraints_value.get("preserve_terms", [])),
            max_chars=int(constraints_value.get("max_chars", 120)),
        ),
    )


def config_from_dict(value: Mapping[str, Any]) -> SummaryConfig:
    limits = value.get("limits", {})
    return SummaryConfig(
        config_version=int(value.get("config_version", 1)),
        precision=str(value.get("precision", "fp32")),
        block_size=int(value.get("block_size", 1)),
        weights={
            str(name): float(weight)
            for name, weight in value.get("weights", {}).items()
        },
        rewrite_strength=float(value.get("rewrite_strength", 0.0)),
        smoothing=float(value.get("smoothing", 0.0)),
        max_sessions_per_stage=int(
            limits.get("max_sessions_per_stage", 10_000)
        ),
        max_resident_frequency=int(
            limits.get("max_resident_frequency", 1_000_000)
        ),
        max_intermediate_memory_bytes=int(
            limits.get("max_intermediate_memory_bytes", 256 * 1024 * 1024)
        ),
        resource_policy=str(value.get("resource_policy", "error")),
    )


def vocabulary_from_dict(value: Mapping[str, Any]) -> Vocabulary:
    return Vocabulary(
        vocab_version=str(value["vocab_version"]),
        words=tuple(str(word) for word in value.get("words", [])),
    )
