"""Public interface for the deterministic motion-temporal pipeline."""

from .aggregation import aggregate_temporal
from .attribution import diagnose_attribution
from .encoding import encode_motion
from .generator import ClipSpec, generate_clip, generate_manifest
from .metrics import evaluate_quality
from .models import FrameSample, TargetObservation
from .pipeline import PipelineConfig, run_batch, run_clip
from .sampling import expand_timeline, sample_frame_pairs
from .selfcheck import validate_pipeline
from .snapshots import SnapshotStore

__all__ = [
    "ClipSpec",
    "FrameSample",
    "PipelineConfig",
    "SnapshotStore",
    "TargetObservation",
    "aggregate_temporal",
    "diagnose_attribution",
    "encode_motion",
    "evaluate_quality",
    "expand_timeline",
    "generate_clip",
    "generate_manifest",
    "run_batch",
    "run_clip",
    "sample_frame_pairs",
    "validate_pipeline",
]

__version__ = "0.1.0"

