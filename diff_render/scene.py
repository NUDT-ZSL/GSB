"""Deterministic procedural scene geometry.

The scene is a heightfield rendered with an orthographic camera looking down
the -Z axis.  Normals are derived analytically from a centered finite
difference of the height function (a deterministic closed-form rule), so the
same description always yields bit-identical geometry.  No random number
generators are used anywhere.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Tuple

import numpy as np


def _height(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Smooth deterministic height field over unit-square coordinates."""
    return (
        0.18 * np.sin(2.0 * math.pi * x) * np.cos(2.0 * math.pi * y)
        + 0.10 * np.sin(4.0 * math.pi * (x + y))
        + 0.06 * np.cos(6.0 * math.pi * (x - 0.5 * y))
    )


def _normal_at(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Unit normal of z = height(x, y) via centered finite differences."""
    epsilon = 1.0e-3
    dh_dx = (_height(x + epsilon, y) - _height(x - epsilon, y)) / (2.0 * epsilon)
    dh_dy = (_height(x, y + epsilon) - _height(x, y - epsilon)) / (2.0 * epsilon)
    # For z = h(x, y) parametrized by (x, y), the upward normal is
    # (-dh/dx, -dh/dy, 1), normalized.
    normal = np.stack((-dh_dx, -dh_dy, np.ones_like(x)), axis=-1)
    norm = np.sqrt(np.sum(normal * normal, axis=-1, keepdims=True))
    return normal / norm


def sample_coordinates(width: int, height: int, samples: int) -> Tuple[np.ndarray, np.ndarray]:
    """Return ``(pixel_index, unit_xy)`` for every deterministic subpixel.

    Subpixels sit on a regular ``samples x samples`` jitter-free grid.  The
    returned pixel index is the canonical flattened index ``row * width + col``
    and rows dominate the order, which is the fixed aggregation order used by
    the whole pipeline.
    """
    # Deterministic regular subpixel grid; take the first ``samples`` cells in
    # row-major order so any positive integer sample count is supported.
    side = int(math.ceil(math.sqrt(samples)))
    offsets = (np.arange(side, dtype=np.float64) + 0.5) / side
    grid_x, grid_y = np.meshgrid(offsets, offsets, indexing="xy")
    sub_x = grid_x.reshape(-1)[:samples]
    sub_y = grid_y.reshape(-1)[:samples]

    cols = np.arange(width, dtype=np.int64)
    rows = np.arange(height, dtype=np.int64)
    pixel_cols, pixel_rows = np.meshgrid(cols, rows, indexing="xy")
    pixel_cols = pixel_cols.reshape(-1)
    pixel_rows = pixel_rows.reshape(-1)
    pixel_index = (pixel_rows * width + pixel_cols).astype(np.int64)

    # Outer loop over pixels (row-major), inner loop over subpixels.
    x = (pixel_cols[:, None] + sub_x[None, :]).reshape(-1) / width
    y = (pixel_rows[:, None] + sub_y[None, :]).reshape(-1) / height
    expanded_pixel_index = np.repeat(pixel_index, samples)
    return expanded_pixel_index, np.stack((x, y), axis=-1)


def build_geometry(config: Dict[str, Any]) -> Dict[str, Any]:
    """Precompute the immutable geometry buffers for a description."""
    camera = config["camera"]
    width = int(camera["width"])
    height = int(camera["height"])
    samples = int(camera["samples_per_pixel"])

    pixel_index, xy = sample_coordinates(width, height, samples)
    normals = _normal_at(xy[:, 0], xy[:, 1])

    lights = []
    for light in config["lights"]:
        direction = np.asarray(light["direction"], dtype=np.float64)
        color = np.asarray(light["color"], dtype=np.float64)
        # Normalize defensively (validation already requires a unit vector).
        lights.append((direction / np.sqrt(np.dot(direction, direction)), color))

    return {
        "width": width,
        "height": height,
        "samples": samples,
        "pixel_count": width * height,
        "sample_count": width * height * samples,
        "pixel_index": pixel_index,
        "normals": normals,
        "view": np.array([0.0, 0.0, 1.0], dtype=np.float64),
        "lights": lights,
        "f0": float(config["material"].get("f0", 0.04)),
        "ambient": float(config["material"].get("ambient", 0.02)),
    }
