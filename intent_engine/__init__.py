"""Deterministic Chinese intent and slot engine.

The package is intentionally self contained.  It only needs the Python
standard library at runtime; numpy is used by the resource generator and is
installed because the exercise asks for it.
"""

from .errors import BusinessError, ConfigMismatchError
from .loader import LoadResult, load_corpus
from .pipeline import PipelineResult, PipelineRunner, run_pipeline
from .resources import ResourceBundle, load_resources

__all__ = [
    "BusinessError",
    "ConfigMismatchError",
    "LoadResult",
    "PipelineResult",
    "PipelineRunner",
    "ResourceBundle",
    "load_corpus",
    "load_resources",
    "run_pipeline",
]

