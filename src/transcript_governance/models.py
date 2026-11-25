"""Core transcript and snapshot domain models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class Word:
    text: str
    start_ms: int
    end_ms: int
    confidence: float = 1.0

    def __post_init__(self) -> None:
        for name in ("start_ms", "end_ms"):
            if not isinstance(getattr(self, name), int):
                object.__setattr__(self, name, int(getattr(self, name)))
        if not isinstance(self.confidence, (int, float)):
            raise TypeError("word confidence must be numeric")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Word":
        return cls(
            text=str(data["text"]),
            start_ms=int(data["start_ms"]),
            end_ms=int(data["end_ms"]),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass(frozen=True)
class Utterance:
    utterance_id: str
    start_ms: int
    end_ms: int
    text: str
    confidence: float = 1.0
    speaker: str | None = None
    words: tuple[Word, ...] = field(default_factory=tuple)
    revision: int = 1
    source_format: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.start_ms, int):
            object.__setattr__(self, "start_ms", int(self.start_ms))
        if not isinstance(self.end_ms, int):
            object.__setattr__(self, "end_ms", int(self.end_ms))
        if not isinstance(self.words, tuple):
            object.__setattr__(self, "words", tuple(self.words))
        if not isinstance(self.confidence, float):
            object.__setattr__(self, "confidence", float(self.confidence))
        if not isinstance(self.revision, int):
            object.__setattr__(self, "revision", int(self.revision))

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def interval(self) -> tuple[int, int]:
        return self.start_ms, self.end_ms

    def overlaps(self, other: "Utterance | tuple[int, int]") -> bool:
        start, end = other.interval() if isinstance(other, Utterance) else other
        return max(self.start_ms, start) < min(self.end_ms, end)

    def overlap_ms(self, other: "Utterance | tuple[int, int]") -> int:
        start, end = other.interval() if isinstance(other, Utterance) else other
        return max(0, min(self.end_ms, end) - max(self.start_ms, start))

    def normalized_text(self) -> str:
        return " ".join(self.text.strip().lower().split())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["words"] = [word.to_dict() if isinstance(word, Word) else word for word in self.words]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Utterance":
        words = tuple(Word.from_dict(item) for item in data.get("words", []))
        return cls(
            utterance_id=str(data["utterance_id"]),
            start_ms=int(data["start_ms"]),
            end_ms=int(data["end_ms"]),
            text=str(data.get("text", "")),
            confidence=float(data.get("confidence", 1.0)),
            speaker=data.get("speaker"),
            words=words,
            revision=int(data.get("revision", 1)),
            source_format=data.get("source_format"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class Snapshot:
    version: int
    checksum: str
    utterances: tuple[Utterance, ...] = field(default_factory=tuple)
    config: dict[str, Any] = field(default_factory=dict)
    declared_regions: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    pcm_hash: str | None = None
    transcript_digests: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
            "utterances": [u.to_dict() for u in self.utterances],
            "config": self.config,
            "declared_regions": [list(region) for region in self.declared_regions],
            "pcm_hash": self.pcm_hash,
            "transcript_digests": self.transcript_digests,
        }


def utterance_map(items: Iterable[Utterance]) -> dict[str, Utterance]:
    return {item.utterance_id: item for item in items}
