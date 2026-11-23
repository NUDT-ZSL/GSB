"""Deterministic customer-support summary and rewrite application."""

from .errors import BusinessError
from .pipeline import PipelineRunner, run_pipeline

__all__ = ["BusinessError", "PipelineRunner", "run_pipeline"]
__version__ = "1.0.0"
