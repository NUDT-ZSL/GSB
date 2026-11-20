"""Tuning-path profiles for scale-adaptive execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import FEATURE_NAMES_FULL, FEATURE_NAMES_REDUCED, DetectionConfig


@dataclass(frozen=True)
class PathProfile:
    name: str
    feature_mode: str
    feature_names: tuple[str, ...]
    analysis_stride_frames: int
    classifier_passes: int
    postprocess_mode: str
    detection_threshold: float

    @property
    def feature_dimension(self) -> int:
        return len(self.feature_names)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "feature_mode": self.feature_mode,
            "feature_dimension": self.feature_dimension,
            "feature_names": list(self.feature_names),
            "analysis_stride_frames": self.analysis_stride_frames,
            "analysis_hop_ms": 10.0 * self.analysis_stride_frames,
            "classifier_passes": self.classifier_passes,
            "postprocess_mode": self.postprocess_mode,
            "detection_threshold": self.detection_threshold,
        }


def path_profiles(detection: DetectionConfig) -> dict[str, PathProfile]:
    return {
        "fine": PathProfile(
            name="fine",
            feature_mode="full",
            feature_names=FEATURE_NAMES_FULL,
            analysis_stride_frames=1,
            classifier_passes=detection.fine_passes,
            postprocess_mode="exact",
            detection_threshold=detection.fine_threshold,
        ),
        "approximate": PathProfile(
            name="approximate",
            feature_mode="reduced",
            feature_names=FEATURE_NAMES_REDUCED,
            analysis_stride_frames=2,
            classifier_passes=1,
            postprocess_mode="threshold",
            detection_threshold=detection.approximate_threshold,
        ),
    }
