"""Whole-grid lighting precomputation.

Distances are measured between walkable cell centers in cell units on the
complete level grid.  Tiles never recompute distances from a local origin,
which guarantees identical gray levels on both sides of every tile seam.
"""
from __future__ import annotations

import numpy as np

from .models import Level


def room_center_cells(level: Level) -> list[tuple[int, int]]:
    centers: list[tuple[int, int]] = []
    room_ids = sorted(set(level.rooms.values()))
    for room_id in room_ids:
        cells = np.array(
            sorted(cell for cell, rid in level.rooms.items() if rid == room_id),
            dtype=np.float64,
        )
        centroid = cells.mean(axis=0)
        distances = ((cells - centroid) ** 2).sum(axis=1)
        nearest = int(np.argmin(distances))
        col, row = cells[nearest].astype(int)
        centers.append((int(col), int(row)))
    return centers


def light_sources(level: Level) -> tuple[tuple[int, int], ...]:
    sources = set(room_center_cells(level))
    sources.update(level.doors)
    walkable = level.walkable()
    sources = {cell for cell in sources if cell in walkable}
    return tuple(sorted(sources))


def nearest_light_distance(level: Level) -> np.ndarray:
    """Return an ``(height, width)`` array, NaN for non-walkable cells."""
    walkable = level.walkable()
    sources = light_sources(level)
    distance = np.full((level.height, level.width), np.nan, dtype=np.float64)
    if not sources:
        for col, row in walkable:
            distance[row, col] = np.inf
        return distance
    source_cols = np.array([cell[0] for cell in sources], dtype=np.float64)
    source_rows = np.array([cell[1] for cell in sources], dtype=np.float64)
    for col, row in walkable:
        deltas = np.hypot(source_cols - col, source_rows - row)
        distance[row, col] = float(deltas.min())
    return distance


def palette_interpolate(level: Level, t_values: np.ndarray) -> np.ndarray:
    """Piecewise-linear palette mapping for normalized attenuation values."""
    positions = np.array([point[0] for point in level.palette], dtype=np.float64)
    grays = np.array([point[1] for point in level.palette], dtype=np.float64)
    return np.interp(t_values, positions, grays)


def walkable_light_factors(level: Level) -> np.ndarray:
    """Attenuation gray target per walkable cell, NaN on wall/void cells."""
    distances = nearest_light_distance(level)
    t = np.clip(distances / level.light_radius, 0.0, 1.0)
    factors = palette_interpolate(level, t)
    return factors
