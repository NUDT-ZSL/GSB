"""Low-level deterministic ray/volume math used by the reference renderer."""

from __future__ import annotations

import numpy as np


def intersect_aabb(
    origins: np.ndarray,
    directions: np.ndarray,
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized slab intersection for ray segment ``[t_enter, t_exit]``.

    The returned ``hit`` mask excludes rays whose intersection has zero length.
    Camera rays are normalized before this call.
    """

    ray_count = origins.shape[0]
    t_enter = np.full(ray_count, -np.inf, dtype=np.float64)
    t_exit = np.full(ray_count, np.inf, dtype=np.float64)
    hit = np.ones(ray_count, dtype=bool)
    for axis in range(3):
        origin = origins[:, axis]
        direction = directions[:, axis]
        low = float(bounds_min[axis])
        high = float(bounds_max[axis])
        nonzero = np.abs(direction) > 1.0e-15
        axis_t0 = np.empty(ray_count, dtype=np.float64)
        axis_t1 = np.empty(ray_count, dtype=np.float64)
        axis_t0[nonzero] = (low - origin[nonzero]) / direction[nonzero]
        axis_t1[nonzero] = (high - origin[nonzero]) / direction[nonzero]
        zero = ~nonzero
        inside = (origin >= low) & (origin <= high)
        axis_t0[zero] = np.where(inside[zero], -np.inf, np.inf)
        axis_t1[zero] = np.where(inside[zero], np.inf, -np.inf)
        t_enter = np.maximum(t_enter, np.minimum(axis_t0, axis_t1))
        t_exit = np.minimum(t_exit, np.maximum(axis_t0, axis_t1))
    t_enter = np.maximum(t_enter, 0.0)
    hit &= np.isfinite(t_exit) & (t_exit > t_enter)
    t_enter = np.where(hit, t_enter, 0.0)
    t_exit = np.where(hit, t_exit, 0.0)
    return t_enter, t_exit, hit


def trilinear_sample(
    data: np.ndarray,
    origin: np.ndarray,
    spacing: np.ndarray,
    positions: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Trilinear scalar value and analytical world-space gradient.

    Returns ``(value, grad_x, grad_y, grad_z)`` for each position.
    """

    depth, height, width = data.shape
    shape_xyz = np.asarray((width, height, depth), dtype=np.float64)
    q = (positions - origin[None, :]) / spacing[None, :]
    q = np.clip(q, 0.0, shape_xyz[None, :] - 1.0)

    base = np.floor(q).astype(np.int64)
    base = np.minimum(base, shape_xyz.astype(np.int64)[None, :] - 2)
    frac = q - base
    x0 = base[:, 0]
    y0 = base[:, 1]
    z0 = base[:, 2]
    x1 = x0 + 1
    y1 = y0 + 1
    z1 = z0 + 1
    fx = frac[:, 0]
    fy = frac[:, 1]
    fz = frac[:, 2]

    c000 = data[z0, y0, x0].astype(np.float64)
    c100 = data[z0, y0, x1].astype(np.float64)
    c010 = data[z0, y1, x0].astype(np.float64)
    c110 = data[z0, y1, x1].astype(np.float64)
    c001 = data[z1, y0, x0].astype(np.float64)
    c101 = data[z1, y0, x1].astype(np.float64)
    c011 = data[z1, y1, x0].astype(np.float64)
    c111 = data[z1, y1, x1].astype(np.float64)

    one_fx = 1.0 - fx
    one_fy = 1.0 - fy
    one_fz = 1.0 - fz

    c00 = c000 * one_fx + c100 * fx
    c01 = c010 * one_fx + c110 * fx
    c10 = c001 * one_fx + c101 * fx
    c11 = c011 * one_fx + c111 * fx
    c0 = c00 * one_fy + c01 * fy
    c1 = c10 * one_fy + c11 * fy
    value = c0 * one_fz + c1 * fz

    gx = (
        (c100 - c000) * one_fy * one_fz
        + (c110 - c010) * fy * one_fz
        + (c101 - c001) * one_fy * fz
        + (c111 - c011) * fy * fz
    )
    gy = (
        (c010 - c000) * one_fx * one_fz
        + (c110 - c100) * fx * one_fz
        + (c011 - c001) * one_fx * fz
        + (c111 - c101) * fx * fz
    )
    gz = (
        (c001 - c000) * one_fx * one_fy
        + (c101 - c100) * fx * one_fy
        + (c011 - c010) * one_fx * fy
        + (c111 - c110) * fx * fy
    )
    gx /= spacing[0]
    gy /= spacing[1]
    gz /= spacing[2]
    return value, gx, gy, gz


def linear_to_srgb(values: np.ndarray) -> np.ndarray:
    """Encode non-negative linear RGB values using the exact sRGB EOTF inverse."""

    clipped = np.clip(values, 0.0, 1.0)
    return np.where(
        clipped <= 0.0031308,
        12.92 * clipped,
        1.055 * np.power(clipped, 1.0 / 2.4) - 0.055,
    )
