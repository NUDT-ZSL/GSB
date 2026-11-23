"""Deterministic event coreference pipeline."""

from .generator import generate_sample
from .pipeline import run_pipeline
from .verify import verify_report

__all__ = ["generate_sample", "run_pipeline", "verify_report"]
__version__ = "0.1.0"

