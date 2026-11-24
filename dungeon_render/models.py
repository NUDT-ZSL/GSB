"""Typed in-memory representations of level and render task documents."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    width: int
    height: int
    # Mapping (col, row) -> room id.  Corridor cells use -1.
    rooms: dict[tuple[int, int], int]
    corridors: frozenset[tuple[int, int]]
    doors: tuple[tuple[int, int], ...]
    wall_thickness: int
    light_radius: float
    palette: tuple[tuple[float, int], ...]

    def walkable(self) -> frozenset[tuple[int, int]]:
        return frozenset(self.rooms) | self.corridors


@dataclass(frozen=True)
class RenderTask:
    output_width: int
    output_height: int
    cell_pixels: int
    tile_cols: int
    tile_rows: int
    concurrency: int
    in_flight_pixel_limit: int
    round_budget: int
    tolerance: int
    state_dir: str
    output_dir: str


@dataclass(frozen=True)
class TileRect:
    """Pixel rectangle of one tile inside the whole output image."""

    col: int
    row: int
    x: int
    y: int
    width: int
    height: int

    @property
    def pixel_count(self) -> int:
        return self.width * self.height


def canonical_level_json(level: Level) -> dict:
    """Deterministic plain-data view used for the version digest."""
    return {
        "width": level.width,
        "height": level.height,
        "rooms": [
            {"cell": list(cell), "room_id": rid}
            for cell, rid in sorted(level.rooms.items())
        ],
        "corridors": [list(cell) for cell in sorted(level.corridors)],
        "doors": [list(cell) for cell in sorted(level.doors)],
        "wall_thickness": level.wall_thickness,
        "light_radius": level.light_radius,
        "palette": [[round(t, 10), g] for t, g in level.palette],
    }


def canonical_task_json(task: RenderTask) -> dict:
    """Deterministic plain-data view; directories do not affect pixels."""
    return {
        "output_width": task.output_width,
        "output_height": task.output_height,
        "cell_pixels": task.cell_pixels,
        "tile_cols": task.tile_cols,
        "tile_rows": task.tile_rows,
        "concurrency": task.concurrency,
        "in_flight_pixel_limit": task.in_flight_pixel_limit,
        "round_budget": task.round_budget,
        "tolerance": task.tolerance,
    }
