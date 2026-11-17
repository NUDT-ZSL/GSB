"""Deterministic incremental vector glyph rasterizer."""

from .errors import EngineError
from .model import Scene, load_scene_bundle

__all__ = ["EngineError", "Scene", "load_scene_bundle"]
__version__ = "1.0.0"
