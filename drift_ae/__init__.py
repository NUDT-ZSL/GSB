"""Deterministic sensor autoencoder and adaptive threshold calibration."""

from .autoencoder import AEConfig, DeterministicAutoencoder
from .calibration import CalibrationRequest, CalibrationService
from .data import FIXTURE_SEED, generate_windows
from .errors import (
    BudgetExceededError,
    ChecksumMismatchError,
    DriftAEError,
    SnapshotRollbackError,
    StageBusyError,
)
from .features import ErrorFeatures, ScoringConfig, extract_features
from .pipeline import DetectionPipeline
from .preprocessing import PreprocessConfig, WindowPreprocessor
from .replay import replay_file, replay_record
from .snapshots import SnapshotStore
from .threshold_formats import (
    downgrade_v2_to_v1,
    load_threshold_model,
    read_v1,
    read_v2,
    upgrade_v1_to_v2,
    write_v1,
    write_v2,
)
from .thresholds import ThresholdModel, ThresholdRow, adjudicate, empirical_quantiles

__version__ = "1.0.0"

__all__ = [
    "AEConfig",
    "BudgetExceededError",
    "CalibrationRequest",
    "CalibrationService",
    "ChecksumMismatchError",
    "DetectionPipeline",
    "DeterministicAutoencoder",
    "DriftAEError",
    "ErrorFeatures",
    "FIXTURE_SEED",
    "PreprocessConfig",
    "ScoringConfig",
    "SnapshotRollbackError",
    "SnapshotStore",
    "StageBusyError",
    "ThresholdModel",
    "ThresholdRow",
    "WindowPreprocessor",
    "__version__",
    "adjudicate",
    "downgrade_v2_to_v1",
    "empirical_quantiles",
    "extract_features",
    "generate_windows",
    "load_threshold_model",
    "read_v1",
    "read_v2",
    "replay_file",
    "replay_record",
    "upgrade_v1_to_v2",
    "write_v1",
    "write_v2",
]
