"""Deterministic local slow-query diagnosis engine."""

from .errors import EngineError
from .sql import normalize_sql

__all__ = ["EngineError", "normalize_sql"]
__version__ = "1.0.0"
