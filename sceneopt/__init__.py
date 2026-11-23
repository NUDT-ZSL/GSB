"""Deterministic scene representation optimization pipeline."""

from .cache import ArtifactCache, CommitGate, build_stage_cache_key
from .config import PipelineConfig, ResourceLimits, ThresholdConfig
from .errors import (
    E_FACE_BUDGET_EXCEEDED,
    E_ERROR_SPECTRUM_INVALID,
    E_INCREMENTAL_INCONSISTENT,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_TEXTURE_BLOCK_MISMATCH,
    E_THRESHOLD_POLICY_INVALID,
    E_TOLERANCE_INVALID,
    SceneOptError,
)
from .mesh import simplify_mesh
from .pipeline import Pipeline, RunResult
from .registration import (
    register_point_cloud,
    registration_residual,
)
from .snapshot import Snapshot, SnapshotStore
from .spectrum import ErrorLevel, ErrorSpectrum, build_error_spectrum
from .texture import TexturedPath, bake_texture_paths, compare_texture_paths
from .thresholds import ThresholdDecision, evaluate_threshold

__all__ = [
    "ArtifactCache",
    "CommitGate",
    "E_ERROR_SPECTRUM_INVALID",
    "E_FACE_BUDGET_EXCEEDED",
    "E_INCREMENTAL_INCONSISTENT",
    "E_RESOURCE_LIMIT",
    "E_SNAPSHOT_CORRUPT",
    "E_SNAPSHOT_ROLLBACK",
    "E_STAGE_BUSY",
    "E_TEXTURE_BLOCK_MISMATCH",
    "E_THRESHOLD_POLICY_INVALID",
    "E_TOLERANCE_INVALID",
    "ErrorLevel",
    "ErrorSpectrum",
    "Pipeline",
    "PipelineConfig",
    "ResourceLimits",
    "RunResult",
    "SceneOptError",
    "Snapshot",
    "SnapshotStore",
    "TexturedPath",
    "ThresholdConfig",
    "ThresholdDecision",
    "bake_texture_paths",
    "build_error_spectrum",
    "build_stage_cache_key",
    "compare_texture_paths",
    "evaluate_threshold",
    "register_point_cloud",
    "registration_residual",
    "simplify_mesh",
]

