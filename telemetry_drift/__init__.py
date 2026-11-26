"""Deterministic telemetry predictor drift diagnostics."""

from .errors import DriftError
from .pipeline import compare_snapshot_runs, run_pipeline

__all__ = ["DriftError", "run_pipeline", "compare_snapshot_runs"]
__version__ = "0.1.0"

