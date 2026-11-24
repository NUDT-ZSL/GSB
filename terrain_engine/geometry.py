"""Global-coordinate gradients, normals and bilinear field sampling.

All difference operators work on the *whole* height field using global grid
indices.  Tiles always call these functions on the same shared array, so a
grid point referenced from any tile (or from a tile boundary) yields exactly
the same normal components.  Edge rows/columns use a deterministic
one-sided finite difference instead of falling back to block-local logic.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

from .model import Terrain


def surface_z(heights: np.ndarray, scale_z: float) -> np.ndarray:
    """Convert integer grid heights to millimetre world z values."""
    return heights.astype(np.float64) * scale_z


def compute_normals(terrain: Terrain) -> np.ndarray:
    """Return unit normals with shape ``(rows_ny, rows_nx, 3)``.

    The surface is ``z(i, j) = h[i, j] * sz`` with
    ``x = i * sx`` and ``y = j * sy``.  Interior cells use central
    differences of the global field; border cells use forward/backward
    one-sided differences.  The gradient is
    ``dz/dx = (dh/di) * sz / sx`` and likewise for y.
    """
    heights = terrain.heights.astype(np.float64)
    rows_ny, rows_nx = heights.shape

    dh_di = np.empty_like(heights)
    if rows_nx == 1:
        dh_di[:] = 0.0
    else:
        # Interior: central difference across global grid columns.
        dh_di[:, 1:-1] = (heights[:, 2:] - heights[:, :-2]) * 0.5
        # Boundary columns: deterministic one-sided differences.
        dh_di[:, 0] = heights[:, 1] - heights[:, 0]
        dh_di[:, -1] = heights[:, -1] - heights[:, -2]

    dh_dj = np.empty_like(heights)
    if rows_ny == 1:
        dh_dj[:] = 0.0
    else:
        dh_dj[1:-1, :] = (heights[2:, :] - heights[:-2, :]) * 0.5
        dh_dj[0, :] = heights[1, :] - heights[0, :]
        dh_dj[-1, :] = heights[-1, :] - heights[-2, :]

    dz_dx = dh_di * terrain.scale_z / terrain.scale_x
    dz_dy = dh_dj * terrain.scale_z / terrain.scale_y

    normals = np.empty((rows_ny, rows_nx, 3), dtype=np.float64)
    normals[..., 0] = -dz_dx
    normals[..., 1] = -dz_dy
    normals[..., 2] = 1.0
    norms = np.linalg.norm(normals, axis=2, keepdims=True)
    # Flat terrain yields norm (0,0,1) -> length 1; guard anyway.
    norms = np.maximum(norms, 1e-300)
    return normals / norms


def _clamped_bilinear_weights(i: np.ndarray, j: np.ndarray,
                              rows_nx: int, rows_ny: int
                              ) -> Tuple[np.ndarray, np.ndarray,
                                         np.ndarray, np.ndarray,
                                         np.ndarray, np.ndarray]:
    i0f = np.floor(i)
    j0f = np.floor(j)
    ti = i - i0f
    tj = j - j0f
    i0 = np.clip(i0f.astype(np.int64), 0, rows_nx - 2)
    j0 = np.clip(j0f.astype(np.int64), 0, rows_ny - 2)
    # When a coordinate lands on the last row/column the floor index equals
    # rows-1; clamp the fractional part so the degenerate cell contributes
    # its single value rather than reading out of range.
    ti = np.where(i0f >= rows_nx - 1, 0.0, ti)
    tj = np.where(j0f >= rows_ny - 1, 0.0, tj)
    return i0, j0, ti, tj, i0 + 1, j0 + 1


def sample_height(surface: np.ndarray, i: np.ndarray,
                  j: np.ndarray) -> np.ndarray:
    """Bilinearly interpolate world-z surface heights at grid indices."""
    rows_ny, rows_nx = surface.shape
    i0, j0, ti, tj, i1, j1 = _clamped_bilinear_weights(
        i, j, rows_nx, rows_ny)
    h00 = surface[j0, i0]
    h10 = surface[j0, i1]
    h01 = surface[j1, i0]
    h11 = surface[j1, i1]
    top = h00 * (1.0 - ti) + h10 * ti
    bottom = h01 * (1.0 - ti) + h11 * ti
    return top * (1.0 - tj) + bottom * tj


def sample_normals(normals: np.ndarray, i: np.ndarray,
                   j: np.ndarray) -> np.ndarray:
    """Bilinearly interpolate normals, then re-normalize per point."""
    rows_ny, rows_nx = normals.shape[:2]
    i0, j0, ti, tj, i1, j1 = _clamped_bilinear_weights(
        i, j, rows_nx, rows_ny)
    n00 = normals[j0, i0]
    n10 = normals[j0, i1]
    n01 = normals[j1, i0]
    n11 = normals[j1, i1]
    ti = ti[..., None]
    tj = tj[..., None]
    top = n00 * (1.0 - ti) + n10 * ti
    bottom = n01 * (1.0 - ti) + n11 * ti
    result = top * (1.0 - tj) + bottom * tj
    lengths = np.linalg.norm(result, axis=-1, keepdims=True)
    return result / np.maximum(lengths, 1e-300)


def grid_indices_from_xy(terrain: Terrain, x: np.ndarray,
                         y: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Inverse world->grid mapping (floating point global grid indices)."""
    return x / terrain.scale_x, y / terrain.scale_y

