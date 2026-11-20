"""Deterministic local columnar execution engine."""

from .engine import Engine, RunSummary
from .errors import EngineError

__all__ = ["Engine", "EngineError", "RunSummary"]
__version__ = "0.1.0"

