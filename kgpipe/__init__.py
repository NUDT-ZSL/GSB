"""Deterministic local knowledge-graph construction pipeline."""

from .errors import KGError
from .pipeline import run_pipeline

__all__ = ["KGError", "run_pipeline"]
__version__ = "0.1.0"

