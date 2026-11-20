"""Local dialogue speech emotion analysis with incremental lineage."""

from .cache import CacheStore, ConfigStore
from .config import (
    AggregationConfig,
    FeatureConfig,
    PipelineConfig,
    RegressionConfig,
    ResourceLimits,
    SpeakerConfig,
)
from .dataset import (
    Dataset,
    DialogRecord,
    EnrollmentRecord,
    build_demo_dataset,
    load_dataset,
    load_dialogs,
    load_enrollments,
    load_truth,
)
from .errors import BusinessError
from .features import FeatureResult, extract_features
from .pipeline import RunResult, VoiceEmotionPipeline, validate_run_consistency
from .regression import aggregate_segments, regress_segment
from .speaker import (
    AssignmentResult,
    SpeakerRegistry,
    assign_speakers,
    build_speaker_registry,
)

__all__ = [
    "AggregationConfig",
    "AssignmentResult",
    "BusinessError",
    "CacheStore",
    "ConfigStore",
    "Dataset",
    "DialogRecord",
    "EnrollmentRecord",
    "FeatureConfig",
    "FeatureResult",
    "PipelineConfig",
    "RegressionConfig",
    "ResourceLimits",
    "RunResult",
    "SpeakerRegistry",
    "SpeakerConfig",
    "VoiceEmotionPipeline",
    "aggregate_segments",
    "assign_speakers",
    "build_demo_dataset",
    "build_speaker_registry",
    "extract_features",
    "load_dataset",
    "load_dialogs",
    "load_enrollments",
    "load_truth",
    "regress_segment",
    "validate_run_consistency",
]
