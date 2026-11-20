"""Typed results exchanged across component boundaries."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .catalog import event_type_id
from .config import FeatureConfig


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass
class FeatureResult:
    dialog_id: str
    features: np.ndarray
    frame_indices: np.ndarray
    sample_starts: np.ndarray
    sample_ends: np.ndarray
    config: FeatureConfig
    pcm_samples: int

    @property
    def frame_count(self) -> int:
        return int(self.features.shape[0])

    @property
    def feature_dimension(self) -> int:
        return int(self.features.shape[1])

    def column_indices(self, names: Sequence[str]) -> list[int]:
        available = list(self.config.feature_names)
        missing = [name for name in names if name not in available]
        if missing:
            raise KeyError(f"unknown feature columns: {missing}")
        return [available.index(name) for name in names]

    def select_columns(self, names: Sequence[str]) -> np.ndarray:
        indices = self.column_indices(names)
        return np.ascontiguousarray(self.features[:, indices], dtype=np.float64)

    def canonical_bytes(self) -> bytes:
        """Stable binary representation used for byte-for-byte determinism checks."""

        header = {
            "dialog_id": self.dialog_id,
            "sample_rate": self.config.sample_rate,
            "frame_ms": self.config.frame_ms,
            "hop_ms": self.config.hop_ms,
            "frame_samples": self.config.frame_samples,
            "hop_samples": self.config.hop_samples,
            "feature_names": list(self.config.feature_names),
            "frame_count": self.frame_count,
            "feature_dimension": self.feature_dimension,
            "pcm_samples": self.pcm_samples,
        }
        header_bytes = _canonical_json_bytes(header)
        matrix = np.ascontiguousarray(self.features, dtype="<f8").tobytes(order="C")
        body = b"AEDF1\0" + struct.pack("<I", len(header_bytes)) + header_bytes + matrix
        return body

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def metadata(self) -> dict[str, Any]:
        return {
            "dialog_id": self.dialog_id,
            "frame_count": self.frame_count,
            "feature_dimension": self.feature_dimension,
            "feature_names": list(self.config.feature_names),
            "feature_sha256": self.sha256(),
            "frame_samples": self.config.frame_samples,
            "hop_samples": self.config.hop_samples,
            "pcm_samples": self.pcm_samples,
        }


@dataclass(frozen=True)
class Candidate:
    start_frame: int
    end_frame: int
    event_type: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "event_type": self.event_type,
            "event_type_id": event_type_id(self.event_type),
            "confidence": round(float(self.confidence), 9),
        }


@dataclass
class DetectionResult:
    dialog_id: str
    frame_labels: list[str]
    frame_scores: list[float]
    candidates: list[Candidate]
    passes: int
    analysis_stride_frames: int
    feature_dimension: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dialog_id": self.dialog_id,
            "passes": self.passes,
            "analysis_stride_frames": self.analysis_stride_frames,
            "feature_dimension": self.feature_dimension,
            "candidate_count": len(self.candidates),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class TimelineSegment:
    """Inclusive frame segment plus exact frame-cell millisecond bounds."""

    start_frame: int
    end_frame: int
    start_ms: float
    end_ms: float
    event_type: str
    confidence: float
    is_event: bool
    segment_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "start_ms": round(float(self.start_ms), 6),
            "end_ms": round(float(self.end_ms), 6),
            "event_type": self.event_type,
            "event_type_id": event_type_id(self.event_type),
            "confidence": round(float(self.confidence), 9),
            "is_event": self.is_event,
        }
