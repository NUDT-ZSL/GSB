"""Deterministic synthetic-layout reading-order analysis engine."""

from .version import ENGINE_VERSION

__version__ = ENGINE_VERSION

from .errors import ConfigError, EngineError, ImageFormatError, SnapshotMismatch
from .engine import EngineRunResult, LayoutEngine

__all__ = [
    "ConfigError",
    "EngineError",
    "EngineRunResult",
    "ImageFormatError",
    "LayoutEngine",
    "SnapshotMismatch",
]
