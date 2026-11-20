"""Deterministic, scale-adaptive audio event detection component."""

from .adaptive import ResourceConstraints, ScaleObservation, decide_path
from .codes import BusinessCode
from .config import (
    DetectionConfig,
    FEATURE_NAMES_FULL,
    FEATURE_NAMES_REDUCED,
    FeatureConfig,
    validate_detection_config,
    validate_feature_config,
)
from .errors import AedError
from .features import extract_features, frame_count_for_samples
from .detection import detect_candidates, validate_candidates
from .metrics import compute_dialog_metrics, compute_frame_metrics
from .paths import PathProfile, path_profiles
from .pipeline import run_dataset
from .postprocess import postprocess_candidates, validate_timeline
from .consistency import run_self_check
from .synthesis import generate_dataset
from .types import Candidate, DetectionResult, FeatureResult, TimelineSegment

__all__ = [
    "AedError",
    "BusinessCode",
    "Candidate",
    "DetectionConfig",
    "DetectionResult",
    "FEATURE_NAMES_FULL",
    "FEATURE_NAMES_REDUCED",
    "FeatureConfig",
    "FeatureResult",
    "PathProfile",
    "ResourceConstraints",
    "ScaleObservation",
    "TimelineSegment",
    "compute_dialog_metrics",
    "compute_frame_metrics",
    "decide_path",
    "detect_candidates",
    "extract_features",
    "frame_count_for_samples",
    "generate_dataset",
    "path_profiles",
    "postprocess_candidates",
    "run_dataset",
    "run_self_check",
    "validate_candidates",
    "validate_detection_config",
    "validate_feature_config",
    "validate_timeline",
]

__version__ = "1.0.0"
