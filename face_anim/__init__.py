"""Deterministic museum exhibition facial-animation pipeline."""

from .errors import PipelineError
from .generator import generate_samples
from .pipeline import run_pipeline

__all__ = ["PipelineError", "generate_samples", "run_pipeline"]
__version__ = "1.0.0"
