"""Tile pyramid generator for game world maps."""

from .core import (
    ConfigError,
    ManifestError,
    SourceError,
    TileWriteError,
    TilemapError,
    compute_max_level,
    generate_tiles,
    level_dimensions,
    load_source,
)

__all__ = [
    "ConfigError",
    "ManifestError",
    "SourceError",
    "TileWriteError",
    "TilemapError",
    "compute_max_level",
    "generate_tiles",
    "level_dimensions",
    "load_source",
]

__version__ = "1.0.0"
