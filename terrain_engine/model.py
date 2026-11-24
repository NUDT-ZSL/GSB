"""Typed in-memory representations of task description and terrain data."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class Terrain:
    """A regular grid height field.

    ``heights[j, i]`` is the integer height at column ``i`` (0..rows_nx-1)
    and row ``j`` (0..rows_ny-1).  World coordinates:
    ``x = i * sx`` and ``y = j * sy``; the height axis is z with scale
    ``sz``, so a grid height value ``h`` maps to world ``z = h * sz``.
    All world units used by the renderer are millimetres.
    """

    rows_nx: int
    rows_ny: int
    scale_x: float
    scale_y: float
    scale_z: float
    heights: np.ndarray  # shape (rows_ny, rows_nx), dtype int64

    @property
    def cell_size_xy(self) -> Tuple[float, float]:
        return self.scale_x, self.scale_y

    def world_xy(self, i: float, j: float) -> Tuple[float, float]:
        return i * self.scale_x, j * self.scale_y


@dataclass(frozen=True)
class Light:
    direction: Tuple[float, float, float]


@dataclass(frozen=True)
class OrthoCamera:
    """Orthographic camera.

    Rays start at ``position`` and travel along the unit *view* vector
    ``target - position``.  The image plane spans ``width`` x ``height``
    millimetres and is oriented with ``up``; ``near`` / ``far`` bound the
    marching interval along the view vector.
    """

    position: Tuple[float, float, float]
    target: Tuple[float, float, float]
    up: Tuple[float, float, float]
    width: float
    height: float
    near: float
    far: float


@dataclass(frozen=True)
class TileSpec:
    """One rectangular output tile given by pixel offsets and size."""

    tile_id: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class TaskSpec:
    terrain: Terrain
    lights: Tuple[Light, Light]
    ambient: float
    camera: OrthoCamera
    width_px: int
    height_px: int
    samples: int
    tile_width: int
    tile_height: int
    max_concurrency: int
    time_budget_ms: float
    memory_budget_bytes: int
    state_dir: str
    ray_step_mm: float
    tiles: Tuple[TileSpec, ...]
    source: Dict[str, object]

    def tile_index(self, tile_id: str) -> int:
        for index, spec in enumerate(self.tiles):
            if spec.tile_id == tile_id:
                return index
        raise KeyError(tile_id)


@dataclass(frozen=True)
class Edit:
    """A single terrain edit in grid coordinates (half-open rectangle)."""

    batch: int
    seq: int
    op: str  # raise | lower | smooth
    x0: int
    y0: int
    x1: int
    y1: int
    amount: int

    def as_rect(self) -> Tuple[int, int, int, int]:
        return self.x0, self.y0, self.x1, self.y1


def finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def normalized(vector: Sequence[float]) -> Tuple[float, float, float]:
    norm = math.sqrt(sum(float(component) ** 2 for component in vector))
    return tuple(float(component) / norm for component in vector)  # type: ignore[return-value]

