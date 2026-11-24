"""Deterministic CPU volumetric renderer.

The public entry point is :func:`volumetric_engine.cli.main`.
"""

from .errors import EngineError
from .model import Scene
from .validation import load_scene

__all__ = ["EngineError", "Scene", "load_scene"]
__version__ = "0.1.0"

