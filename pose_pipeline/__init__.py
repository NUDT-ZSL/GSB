"""Deterministic multi-stage human pose pipeline."""

from .errors import PipelineError
from .pipeline import run_pipeline

__all__ = ["PipelineError", "run_pipeline"]
__version__ = "1.0.0"
