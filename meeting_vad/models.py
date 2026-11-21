"""Input data models for clips and annotations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Clip:
    clip_id: str
    sample_rate: int
    duration_ms: float
    seed: int
    frame_ms: float
    audio_pcm: str
    audio_wav: str | None
    channels: int
    sample_width: int
    expected_samples: int
    pcm_sha256: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class Annotation:
    clip_id: str
    segment_id: str
    start_ms: float
    end_ms: float
    speaker_label: str
    label_revision: int
    confidence: float
    confidence_lower: float | None = None
    confidence_upper: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "clip_id": self.clip_id,
            "segment_id": self.segment_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "speaker_label": self.speaker_label,
            "label_revision": self.label_revision,
            "confidence": self.confidence,
        }
        if self.confidence_lower is not None:
            result["confidence_lower"] = self.confidence_lower
        if self.confidence_upper is not None:
            result["confidence_upper"] = self.confidence_upper
        return result


@dataclass(frozen=True)
class AudioInfo:
    clip: Clip
    samples: Any
    total_samples: int
    pcm_sha256: str
    chunk_sample_count: int
    chunk_hashes: tuple[str, ...]
    chunk_manifest_hash: str
