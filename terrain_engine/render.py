"""Per-supersample surface intersection and Lambert shading.

The kernel is fully deterministic and side-effect free apart from reading the
shared height field: repeated rendering of the same tile always yields
byte-identical gray/depth arrays, independent of the thread that executes it.
Rays march in millimetres along the orthographic view direction and use a
fixed step followed by bisection refinement to locate the first surface
crossing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

import numpy as np

from . import geometry
from .model import OrthoCamera, TaskSpec, Terrain, TileSpec, normalized


# Per sub-sample bookkeeping kept in the in-flight sample buffer:
# ray origin (3), current position (3), distance, hit flag.
FLOATS_PER_SAMPLE = 8


@dataclass(frozen=True)
class CameraBasis:
    forward: Tuple[float, float, float]
    right: Tuple[float, float, float]
    up_axis: Tuple[float, float, float]


def camera_basis(camera: OrthoCamera) -> CameraBasis:
    forward = normalized(
        tuple(camera.target[k] - camera.position[k] for k in range(3)))
    up = normalized(camera.up)
    right = normalized(np.cross(forward, up).tolist())
    # Re-orthogonalize up so the frame is an exact orthonormal basis.
    up_axis = normalized(np.cross(right, forward).tolist())
    return CameraBasis(forward, tuple(right), tuple(up_axis))


def _halton(index: int, base: int) -> float:
    result = 0.0
    fraction = 1.0
    value = index
    while value > 0:
        fraction /= base
        result += fraction * (value % base)
        value //= base
    return result


def subpixel_offsets(samples: int) -> np.ndarray:
    """Deterministic low-discrepancy sub-pixel offsets in ``[0, 1)``."""
    offsets = np.empty((samples, 2), dtype=np.float64)
    for index in range(samples):
        # Skip the (0, 0) first Halton pair for a centred distribution.
        sequence_index = index + 1
        offsets[index, 0] = _halton(sequence_index, 2) - 0.5
        offsets[index, 1] = _halton(sequence_index, 3) - 0.5
    return offsets


def ray_origins(camera: OrthoCamera, basis: CameraBasis, tile: TileSpec,
                width_px: int, height_px: int,
                samples: int) -> np.ndarray:
    """Return ray origins shaped ``(tile_h, tile_w, samples, 3)``."""
    offsets = subpixel_offsets(samples)
    px_x = tile.x + np.arange(tile.width, dtype=np.float64)
    px_y = tile.y + np.arange(tile.height, dtype=np.float64)
    grid_x, grid_y = np.meshgrid(px_x, px_y)
    # Pixel centres in camera-plane millimetres; +y maps to camera up.
    cam_x = (grid_x + 0.5 - width_px / 2.0)
    cam_x *= camera.width / width_px
    cam_y = (height_px / 2.0 - (grid_y + 0.5))
    cam_y *= camera.height / height_px

    origins = np.empty((tile.height, tile.width, samples, 3),
                       dtype=np.float64)
    position = np.asarray(camera.position, dtype=np.float64)
    right = np.asarray(basis.right, dtype=np.float64)
    up_axis = np.asarray(basis.up_axis, dtype=np.float64)
    pixel_size_x = camera.width / width_px
    pixel_size_y = camera.height / height_px
    for sample_index in range(samples):
        offset_x, offset_y = offsets[sample_index]
        plane_x = cam_x + offset_x * pixel_size_x
        plane_y = cam_y + offset_y * pixel_size_y
        origins[..., sample_index, :] = (
            position
            + plane_x[..., None] * right
            + plane_y[..., None] * up_axis)
    return origins


def sample_buffer_bytes(tile: TileSpec, samples: int) -> int:
    """Deterministic in-flight sample buffer size (float64 bookkeeping)."""
    pixel_count = tile.width * tile.height
    return pixel_count * samples * FLOATS_PER_SAMPLE * np.dtype(
        np.float64).itemsize


def march_rays(task: TaskSpec, tile: TileSpec,
               basis: CameraBasis) -> Tuple[np.ndarray, np.ndarray,
                                            np.ndarray]:
    """Intersect every sub-sample ray with the height field.

    Returns ``(hit_mask, distance, hit_points)`` where distance is measured
    in millimetres along the view ray and ``hit_points`` holds world xyz.
    Background rays keep distance 0 and mask value False.
    """
    terrain = task.terrain
    surface = geometry.surface_z(terrain.heights, terrain.scale_z)
    origins = ray_origins(task.camera, basis, tile, task.width_px,
                          task.height_px, task.samples)
    forward = np.asarray(basis.forward, dtype=np.float64)
    span = task.camera.far - task.camera.near
    step_count = max(1, int(math.ceil(span / task.ray_step_mm)))
    distances = np.linspace(
        task.camera.near, task.camera.far, step_count + 1,
        dtype=np.float64)

    shape = origins.shape[:-1]
    hit_mask = np.zeros(shape, dtype=bool)
    hit_distance = np.zeros(shape, dtype=np.float64)
    hit_points = np.zeros(shape + (3,), dtype=np.float64)

    def terrain_z_at(points: np.ndarray) -> np.ndarray:
        grid_i, grid_j = geometry.grid_indices_from_xy(
            terrain, points[..., 0], points[..., 1])
        return geometry.sample_height(surface, grid_i, grid_j)

    previous_points = origins + forward * distances[0]
    previous_below = previous_points[..., 2] < terrain_z_at(previous_points)
    previous_distance = np.full(shape, distances[0], dtype=np.float64)

    for step in range(1, len(distances)):
        current_distance = distances[step]
        current_points = origins + forward * current_distance
        current_below = current_points[..., 2] < terrain_z_at(current_points)
        crossing = current_below & ~previous_below & ~hit_mask
        if np.any(crossing):
            low = previous_distance[crossing]
            high = np.full(low.shape, current_distance, dtype=np.float64)
            low_points = previous_points[crossing]
            high_points = current_points[crossing]
            ray_starts = origins[crossing]
            # Bisection refinement on the crossing bracket.
            for _ in range(6):
                middle = (low + high) * 0.5
                middle_points = ray_starts + forward * middle[:, None]
                middle_below = (
                    middle_points[..., 2]
                    < terrain_z_at(middle_points))
                high = np.where(middle_below, middle, high)
                low = np.where(middle_below, low, middle)
                high_points = np.where(
                    middle_below[:, None], middle_points, high_points)
                low_points = np.where(
                    middle_below[:, None], low_points, middle_points)
            hit_distance[crossing] = (low + high) * 0.5
            refined = ray_starts + forward * hit_distance[crossing, None]
            hit_points[crossing] = refined
            hit_mask[crossing] = True
        previous_points = current_points
        previous_below = current_below
        previous_distance = np.full(shape, current_distance,
                                    dtype=np.float64)
    return hit_mask, hit_distance, hit_points


def shade(task: TaskSpec, hit_points: np.ndarray,
          hit_mask: np.ndarray, normals_field: np.ndarray) -> np.ndarray:
    """Two-light Lambert shading plus ambient, clamped to ``[0, 1]``."""
    terrain = task.terrain
    grid_i, grid_j = geometry.grid_indices_from_xy(
        terrain, hit_points[..., 0], hit_points[..., 1])
    interpolated = geometry.sample_normals(
        normals_field, grid_i, grid_j)
    intensity = np.full(hit_mask.shape, task.ambient, dtype=np.float64)
    for light in task.lights:
        direction = np.asarray(light.direction, dtype=np.float64)
        diffuse = np.sum(interpolated * direction, axis=-1)
        intensity += np.maximum(diffuse, 0.0)
    intensity = np.clip(intensity, 0.0, 1.0)
    return np.where(hit_mask, intensity, 0.0)


def render_tile(task: TaskSpec, tile: TileSpec) -> Tuple[np.ndarray,
                                                         np.ndarray,
                                                         np.ndarray]:
    """Render one tile.

    Returns ``(gray, depth, sample_buffer)`` where gray is uint8, depth is
    float32 world-unit ray distance (background = -1.0) and the sample
    buffer is the deterministic in-flight working set retained for memory
    accounting.
    """
    basis = camera_basis(task.camera)
    hit_mask, hit_distance, hit_points = march_rays(task, tile, basis)
    normals_field = geometry.compute_normals(task.terrain)
    intensity = shade(task, hit_points, hit_mask, normals_field)

    pixel_intensity = intensity.mean(axis=-1)
    gray = np.rint(pixel_intensity * 255.0).astype(np.uint8)
    # Depth is the mean distance over the sub-samples that actually hit;
    # only a pixel with no surface intersection at all stays background.
    hit_count = np.maximum(hit_mask.sum(axis=-1), 1)
    mean_hit_distance = (
        (hit_distance * hit_mask).sum(axis=-1) / hit_count)
    depth = np.where(
        hit_mask.any(axis=-1),
        mean_hit_distance,
        -1.0).astype(np.float32)

    sample_buffer = np.concatenate(
        [hit_points.reshape(-1, 3),
         hit_distance.reshape(-1, 1),
         hit_mask.reshape(-1, 1).astype(np.float64)],
        axis=1)
    return gray, depth, sample_buffer


def tile_grid_extent(task: TaskSpec, tile: TileSpec
                     ) -> Tuple[int, int, int, int]:
    """Conservative global grid-index range read while rendering a tile.

    The four tile frustum corners are marched over the full near/far span;
    their x/y extrema are converted to global grid indices and expanded by
    one cell on each side to cover the stencil of bilinear/normal access.
    This is the tile's dependency closure used for edit invalidation.
    """
    terrain: Terrain = task.terrain
    basis = camera_basis(task.camera)
    origins = ray_origins(task.camera, basis, tile, task.width_px,
                          task.height_px, 1)
    forward = np.asarray(basis.forward, dtype=np.float64)
    corners = origins[:, :, 0, :].reshape(-1, 3)
    near_points = corners + forward * task.camera.near
    far_points = corners + forward * task.camera.far
    points = np.concatenate([near_points, far_points], axis=0)
    grid_i, grid_j = geometry.grid_indices_from_xy(
        terrain, points[..., 0], points[..., 1])
    min_i = int(math.floor(float(np.min(grid_i)))) - 1
    max_i = int(math.ceil(float(np.max(grid_i)))) + 1
    min_j = int(math.floor(float(np.min(grid_j)))) - 1
    max_j = int(math.ceil(float(np.max(grid_j)))) + 1
    min_i = max(0, min_i)
    min_j = max(0, min_j)
    max_i = min(terrain.rows_nx - 1, max_i)
    max_j = min(terrain.rows_ny - 1, max_j)
    return min_i, min_j, max_i, max_j

