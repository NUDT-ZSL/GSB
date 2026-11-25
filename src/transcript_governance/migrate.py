"""Bidirectional v1/v2 migration with explicit defaults and loss rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

from .models import Utterance, Word
from .results import LoadResult


@dataclass(frozen=True)
class FillRecord:
    utterance_id: str
    field_name: str
    value: object
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "utterance_id": self.utterance_id,
            "field": self.field_name,
            "value": self.value,
            "source": self.source,
        }


@dataclass(frozen=True)
class LossRecord:
    utterance_id: str
    field_name: str
    rule: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "utterance_id": self.utterance_id,
            "field": self.field_name,
            "rule": self.rule,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BoundaryPair:
    source_id: str
    target_id: str
    start_ms: int
    end_ms: int
    preserved: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "preserved": self.preserved,
        }


@dataclass
class MigrationResult:
    target_format: str
    utterances: list[Utterance]
    filled_defaults: list[FillRecord] = field(default_factory=list)
    loss_records: list[LossRecord] = field(default_factory=list)
    boundary_pairs: list[BoundaryPair] = field(default_factory=list)

    @property
    def boundaries_preserved(self) -> bool:
        return all(item.preserved for item in self.boundary_pairs)

    def to_dict(self) -> dict[str, object]:
        return {
            "target_format": self.target_format,
            "utterance_count": len(self.utterances),
            "boundaries_preserved": self.boundaries_preserved,
            "filled_defaults": [item.to_dict() for item in self.filled_defaults],
            "loss_records": [item.to_dict() for item in self.loss_records],
            "boundary_pairs": [item.to_dict() for item in self.boundary_pairs],
            "utterances": [item.to_dict() for item in self.utterances],
        }


@dataclass(frozen=True)
class V1ToV2Policy:
    default_speaker: str = "S0"
    default_revision: int = 1
    word_timing: Literal["empty", "even_allocate"] = "empty"


@dataclass(frozen=True)
class V2ToV1Policy:
    speaker_rule: Literal["discard", "flatten_prefix"] = "discard"
    words_rule: Literal["flatten_text", "discard", "flatten_timing_tokens"] = "flatten_text"
    revision_rule: Literal["discard", "flatten_id_suffix"] = "discard"
    default_confidence: float = 1.0


def _items(source: LoadResult | Iterable[Utterance]) -> list[Utterance]:
    if isinstance(source, LoadResult):
        return list(source.utterances)
    return list(source)


def _even_words(text: str, start_ms: int, end_ms: int) -> tuple[Word, ...]:
    tokens = text.split()
    if not tokens:
        return ()
    total = max(1, end_ms - start_ms)
    weights = [max(1, len(token)) for token in tokens]
    weight_sum = sum(weights)
    cursor = start_ms
    words: list[Word] = []
    for index, (token, weight) in enumerate(zip(tokens, weights)):
        if index == len(tokens) - 1:
            word_end = end_ms
        else:
            word_end = start_ms + round(total * sum(weights[: index + 1]) / weight_sum)
        word_end = max(cursor, min(end_ms, word_end))
        words.append(Word(text=token, start_ms=cursor, end_ms=word_end, confidence=1.0))
        cursor = word_end
    if words and words[-1].end_ms != end_ms:
        last = words[-1]
        words[-1] = Word(last.text, last.start_ms, end_ms, last.confidence)
    return tuple(words)


def migrate_v1_to_v2(
    source: LoadResult | Iterable[Utterance],
    policy: V1ToV2Policy = V1ToV2Policy(),
) -> MigrationResult:
    source_items = _items(source)
    migrated: list[Utterance] = []
    fills: list[FillRecord] = []
    boundaries: list[BoundaryPair] = []

    for item in source_items:
        speaker = item.speaker
        if speaker is None:
            speaker = policy.default_speaker
            fills.append(FillRecord(item.utterance_id, "speaker", speaker, "default:V1ToV2Policy.default_speaker"))

        revision = item.revision if "revision" in item.metadata else policy.default_revision
        if "revision" not in item.metadata:
            revision = policy.default_revision
            fills.append(FillRecord(item.utterance_id, "revision", revision, "default:V1ToV2Policy.default_revision"))

        words = item.words
        if not words:
            if policy.word_timing == "even_allocate" and item.text.strip():
                words = _even_words(item.text, item.start_ms, item.end_ms)
                fills.append(
                    FillRecord(
                        item.utterance_id,
                        "words",
                        f"{len(words)} words",
                        "inferred:even-character-weight-allocation",
                    )
                )
            else:
                words = ()
                fills.append(FillRecord(item.utterance_id, "words", [], "default:empty-word-timing-list"))

        confidence = item.confidence
        if confidence is None:
            confidence = 1.0
            fills.append(FillRecord(item.utterance_id, "confidence", 1.0, "default:confidence=1.0"))

        target = Utterance(
            utterance_id=item.utterance_id,
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            text=item.text,
            confidence=confidence,
            speaker=speaker,
            words=words,
            revision=revision,
            source_format="v2-migrated-from-v1",
            metadata={**item.metadata, "migrated_from": "v1"},
        )
        migrated.append(target)
        boundaries.append(
            BoundaryPair(
                item.utterance_id,
                target.utterance_id,
                target.start_ms,
                target.end_ms,
                item.start_ms == target.start_ms and item.end_ms == target.end_ms,
            )
        )

    return MigrationResult("v2", migrated, fills, [], boundaries)


def migrate_v2_to_v1(
    source: LoadResult | Iterable[Utterance],
    policy: V2ToV1Policy = V2ToV1Policy(),
) -> MigrationResult:
    source_items = _items(source)
    migrated: list[Utterance] = []
    fills: list[FillRecord] = []
    losses: list[LossRecord] = []
    boundaries: list[BoundaryPair] = []

    for item in source_items:
        target_id = item.utterance_id
        text = item.text
        speaker_prefix: str | None = None

        if item.speaker is not None:
            if policy.speaker_rule == "flatten_prefix":
                speaker_prefix = f"[{item.speaker}]"
                losses.append(
                    LossRecord(item.utterance_id, "speaker", "flatten_prefix", "speaker moved into text prefix")
                )
            else:
                losses.append(
                    LossRecord(item.utterance_id, "speaker", "discard", f"dropped speaker={item.speaker}")
                )

        if item.words:
            if policy.words_rule == "flatten_text":
                text = " ".join(word.text for word in item.words)
                losses.append(
                    LossRecord(item.utterance_id, "word_timing", "flatten_text", "word forms retained; millisecond timing dropped")
                )
            elif policy.words_rule == "flatten_timing_tokens":
                text = " ".join(f"{word.text}[{word.start_ms}-{word.end_ms}]" for word in item.words)
                losses.append(
                    LossRecord(item.utterance_id, "word_timing", "flatten_timing_tokens", "timing encoded in text tokens")
                )
            else:
                losses.append(
                    LossRecord(item.utterance_id, "words", "discard", f"dropped {len(item.words)} word timings")
                )
        if speaker_prefix is not None:
            text = f"{speaker_prefix} {text}"

        if policy.revision_rule == "flatten_id_suffix":
            target_id = f"{item.utterance_id}#r{item.revision}"
            losses.append(
                LossRecord(item.utterance_id, "revision", "flatten_id_suffix", "revision appended to utterance_id")
            )
        else:
            losses.append(
                LossRecord(item.utterance_id, "revision", "discard", f"dropped revision={item.revision}")
            )

        confidence = item.confidence
        if item.metadata.get("confidence_defaulted"):
            confidence = policy.default_confidence
            fills.append(
                FillRecord(item.utterance_id, "confidence", confidence, "default:reader.optional_confidence=1.0")
            )
        elif confidence is None:
            confidence = policy.default_confidence
            fills.append(
                FillRecord(item.utterance_id, "confidence", confidence, "default:V2ToV1Policy.default_confidence")
            )

        target = Utterance(
            utterance_id=target_id,
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            text=text,
            confidence=confidence,
            speaker=None,
            words=(),
            revision=1,
            source_format="v1-migrated-from-v2",
            metadata={**item.metadata, "migrated_from": "v2"},
        )
        migrated.append(target)
        boundaries.append(
            BoundaryPair(
                item.utterance_id,
                target.utterance_id,
                target.start_ms,
                target.end_ms,
                item.start_ms == target.start_ms and item.end_ms == target.end_ms,
            )
        )

    return MigrationResult("v1", migrated, fills, losses, boundaries)
