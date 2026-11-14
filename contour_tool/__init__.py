"""Contour extraction and query tool for regular grid scalar fields."""

from .contour import LevelContours, QueryHit, extract_all, extract_level, level_colors, locate
from .errors import OutputError, SceneError
from .report import build_result
from .scene import load_scene, parse_scene

__all__ = [
    "LevelContours",
    "OutputError",
    "QueryHit",
    "SceneError",
    "build_result",
    "extract_all",
    "extract_level",
    "level_colors",
    "load_scene",
    "locate",
    "parse_scene",
]
