"""Deterministic surface-tile registration and stitching pipeline."""

from .errors import PipelineError
from .pipeline import PipelineConfig, run_pipeline

__version__ = "1.0.0"

__all__ = ["PipelineError", "PipelineConfig", "run_pipeline", "__version__"]
