"""Deterministic local metric ingestion engine."""

from .engine import Engine
from .errors import EngineError

__all__ = ["Engine", "EngineError"]
__version__ = "0.1.0"

