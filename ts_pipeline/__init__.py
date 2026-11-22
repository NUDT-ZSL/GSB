"""Deterministic time-series precision and error-propagation pipeline."""

from .config import DEFAULT_CONFIG, load_config, normalize_config
from .consistency import verify_report_consistency
from .errors import (
    ConfigInvalid,
    ErrorSpectrumInvalid,
    InconsistentError,
    PipelineError,
    RecursionInvalid,
    SnapshotCorrupt,
    SnapshotRollback,
)
from .pipeline import run_pipeline, run_pipeline_from_config
from .evaluation import evaluate_metrics, threshold_decision
from .forecast import forecast_windows
from .precision import Quantizer, quantization_audit, quantize_fixed_scalar
from .scaling import fit_feature_scaler
from .series import SeriesData, generate_series, load_series, save_series
from .snapshots import SnapshotStore
from .spectrum import (
    CONTROLLED_LEVELS,
    build_error_spectrum,
    validate_error_spectrum,
)
from .training import train_models
from .windows import build_windows

__all__ = [
    "DEFAULT_CONFIG",
    "ConfigInvalid",
    "CONTROLLED_LEVELS",
    "ErrorSpectrumInvalid",
    "InconsistentError",
    "PipelineError",
    "Quantizer",
    "RecursionInvalid",
    "SeriesData",
    "SnapshotCorrupt",
    "SnapshotRollback",
    "SnapshotStore",
    "build_error_spectrum",
    "build_windows",
    "evaluate_metrics",
    "fit_feature_scaler",
    "forecast_windows",
    "generate_series",
    "load_config",
    "load_series",
    "normalize_config",
    "quantization_audit",
    "quantize_fixed_scalar",
    "run_pipeline",
    "run_pipeline_from_config",
    "save_series",
    "threshold_decision",
    "train_models",
    "validate_error_spectrum",
    "verify_report_consistency",
]

__version__ = "1.0.0"
