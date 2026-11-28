"""Internal normalized data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PhonemeEvent:
    id: str
    source_label: str
    canonical: str
    category: str
    viseme: str
    start_us: int
    end_us: int
    targets: dict[str, float]

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    @property
    def center_us(self) -> int:
        return (self.start_us + self.end_us) // 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_label": self.source_label,
            "canonical": self.canonical,
            "category": self.category,
            "viseme": self.viseme,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "start_ms": round(self.start_us / 1000.0, 6),
            "end_ms": round(self.end_us / 1000.0, 6),
            "duration_ms": round(self.duration_us / 1000.0, 6),
            "targets": dict(self.targets),
        }


@dataclass
class StressMark:
    phoneme_id: str
    level: float


@dataclass
class Sentence:
    id: str
    text: str
    start_us: int
    end_us: int
    emotion_label: str
    emotion_intensity: float
    emotion_anchor_id: str | None
    stress: list[StressMark]
    phonemes: list[PhonemeEvent]

    def phoneme_by_id(self, phoneme_id: str) -> PhonemeEvent | None:
        for phoneme in self.phonemes:
            if phoneme.id == phoneme_id:
                return phoneme
        return None


@dataclass
class VersionData:
    version: str
    label_style: str
    time_base: str
    source_fps: float | None
    audio_path: Path
    audio_sample_rate: int
    audio_duration_us: int
    duration_us: int
    sentences: list[Sentence]
    channel_sources: dict[str, str]
    annotation_path: Path
    bindings_path: Path
    channel_binding_details: dict[str, dict[str, Any]] = field(default_factory=dict)

    def phonemes(self) -> list[PhonemeEvent]:
        return [phoneme for sentence in self.sentences for phoneme in sentence.phonemes]

    def sentence(self, sentence_id: str) -> Sentence | None:
        for sentence in self.sentences:
            if sentence.id == sentence_id:
                return sentence
        return None


@dataclass
class TruthData:
    path: Path | None
    duration_us: int
    sentences: list[Sentence]
    source: str

    def phonemes(self) -> list[PhonemeEvent]:
        return [phoneme for sentence in self.sentences for phoneme in sentence.phonemes]

    def sentence(self, sentence_id: str) -> Sentence | None:
        for sentence in self.sentences:
            if sentence.id == sentence_id:
                return sentence
        return None


@dataclass
class SmoothTrack:
    sample_rate_hz: int
    sample_times_us: list[int]
    values: dict[str, list[float]]
    duration_us: int


@dataclass
class FrameCurves:
    fps: float
    frame_count: int
    frame_times_us: list[int]
    channels: tuple[str, ...]
    values: Any  # numpy.ndarray shape (frame_count, channel_count)
