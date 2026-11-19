"""Deterministic, script-driven TCP trajectory engine."""

from .errors import EngineError
from .scene import load_scene

__all__ = ["EngineError", "load_scene"]
__version__ = "1.0.0"

