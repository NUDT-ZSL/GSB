"""Deterministic binning, encoding, caching and recovery engine."""

from .codes import ErrorCode, EngineError
from .config import load_config, normalize_config
from .pipeline import EngineResult, run_pipeline
from .reference import reference_encode
from .scheduler import BatchScheduler

__all__ = [
    "ErrorCode",
    "EngineError",
    "load_config",
    "normalize_config",
    "run_pipeline",
    "reference_encode",
    "BatchScheduler",
    "EngineResult",
]

__version__ = "1.0.0"
