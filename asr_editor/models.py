"""Unified utterance/token model with integer-millisecond timing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Origin:
    """Information retained from the source representation."""

    source_version: int
    start_ms: int | None
    end_ms: int | None
    text: str | None
    start_raw: str | None = None
    end_raw: str | None = None
    source_file: str | None = None
    source_line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "start_raw": self.start_raw,
            "end_raw": self.end_raw,
            "source_file": self.source_file,
            "source_line": self.source_line,
        }


@dataclass(slots=True)
class Token:
    text: str
    start_ms: int
    end_ms: int
    inferred: bool = False
    origin: Origin | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "inferred": self.inferred,
            "origin": self.origin.to_dict() if self.origin else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Token:
        origin_data = data.get("origin")
        return cls(
            text=str(data["text"]),
            start_ms=int(data["start_ms"]),
            end_ms=int(data["end_ms"]),
            inferred=bool(data.get("inferred", False)),
            origin=Origin(**origin_data) if origin_data else None,
        )


@dataclass(slots=True)
class Utterance:
    uid: str
    start_ms: int
    end_ms: int
    tokens: list[Token] = field(default_factory=list)
    origin: Origin | None = None

    @property
    def text(self) -> str:
        return " ".join(token.text for token in self.tokens)

    @property
    def inferred_time_count(self) -> int:
        return sum(1 for token in self.tokens if token.inferred)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.uid,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "origin": self.origin.to_dict() if self.origin else None,
            "tokens": [token.to_dict() for token in self.tokens],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Utterance:
        origin_data = data.get("origin")
        return cls(
            uid=str(data["id"]),
            start_ms=int(data["start_ms"]),
            end_ms=int(data["end_ms"]),
            tokens=[Token.from_dict(item) for item in data.get("tokens", [])],
            origin=Origin(**origin_data) if origin_data else None,
        )

    def reflow_bounds(self) -> None:
        if self.tokens:
            self.start_ms = self.tokens[0].start_ms
            self.end_ms = self.tokens[-1].end_ms


@dataclass(slots=True)
class Transcript:
    source_version: int
    utterances: list[Utterance] = field(default_factory=list)
    language: str | None = None
    timing_origin: str = "v1"

    @property
    def inferred_time_count(self) -> int:
        return sum(utt.inferred_time_count for utt in self.utterances)

    @property
    def token_count(self) -> int:
        return sum(len(utt.tokens) for utt in self.utterances)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_version": self.source_version,
            "timing_origin": self.timing_origin,
            "language": self.language,
            "utterances": [utt.to_dict() for utt in self.utterances],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        return cls(
            source_version=int(data["source_version"]),
            utterances=[
                Utterance.from_dict(item) for item in data.get("utterances", [])
            ],
            language=data.get("language"),
            timing_origin=str(data.get("timing_origin", "v1")),
        )

    def utterance_by_id(self, uid: str) -> Utterance | None:
        return next((utt for utt in self.utterances if utt.uid == uid), None)

    def iter_tokens(self) -> Iterable[tuple[int, Utterance, int, Token]]:
        for utterance_index, utterance in enumerate(self.utterances):
            for token_index, token in enumerate(utterance.tokens):
                yield utterance_index, utterance, token_index, token

    def normalized_copy(self) -> Transcript:
        return Transcript.from_dict(self.to_dict())
