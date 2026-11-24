"""Deterministic tile partitioning and row-major tile indexing."""
from __future__ import annotations

from .models import RenderTask, TileRect


def build_tiles(task: RenderTask) -> list[TileRect]:
    """Partition the output into a complete ``tile_cols x tile_rows`` grid.

    Tiles are returned in row-major order; all geometry still uses whole
    image pixel coordinates, so uneven widths are handled without seams.
    """
    tiles: list[TileRect] = []
    width = task.output_width
    height = task.output_height
    for row in range(task.tile_rows):
        y0 = row * height // task.tile_rows
        y1 = (row + 1) * height // task.tile_rows
        for col in range(task.tile_cols):
            x0 = col * width // task.tile_cols
            x1 = (col + 1) * width // task.tile_cols
            tiles.append(
                TileRect(
                    col=col,
                    row=row,
                    x=x0,
                    y=y0,
                    width=x1 - x0,
                    height=y1 - y0,
                )
            )
    return tiles


def tile_index(task: RenderTask, tile: TileRect) -> int:
    return tile.row * task.tile_cols + tile.col
