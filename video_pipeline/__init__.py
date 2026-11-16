"""Deterministic offline video detection and tracking pipeline."""

from .config import DetectionConfig, MatchingConfig, load_detection_config, load_matching_config
from .pipeline import Pipeline, PipelineResult

__all__ = [
    "DetectionConfig",
    "MatchingConfig",
    "Pipeline",
    "PipelineResult",
    "load_detection_config",
    "load_matching_config",
]

__version__ = "1.0.0"
