"""Offline deterministic dungeon level renderer and seam validator.

The package only depends on the Python standard library and NumPy.  All
coordinate math is performed in whole-image pixel space so that tiling,
worker scheduling and traversal order cannot affect rendered pixels.
"""
from .errors import DungeonError
from .models import Level, RenderTask, TileRect
from .pipeline import run_pipeline

__all__ = [
    "DungeonError",
    "Level",
    "RenderTask",
    "TileRect",
    "run_pipeline",
]

EXIT_OK = 0
EXIT_SCHEMA = 2
EXIT_DIGEST = 3
EXIT_SEAM = 4
EXIT_INTERRUPT = 130
