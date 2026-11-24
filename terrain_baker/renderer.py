from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

import numpy as np

from .graph import ChunkSpec
from .heightfield import DependencyTracker, GlobalHeightField


SAMPLE_BUFFER_BYTES_PER_PIXEL = 768
SAMPLE_BUFFER_FIXED_BYTES = 65536


def estimate_sample_buffer_bytes(width: int, height: int) -> int:
    return int(width) * int(height) * SAMPLE_BUFFER_BYTES_PER_PIXEL + SAMPLE_BUFFER_FIXED_BYTES


def camera_basis(camera: Dict[str, Any]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    position = np.asarray(camera["position"], dtype=np.float64)
    target = np.asarray(camera["target"], dtype=np.float64)
    up_hint = np.asarray(camera["up"], dtype=np.float64)
    forward = target - position
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, up_hint)
    right /= np.linalg.norm(right)
    true_up = np.cross(right, forward)
    true_up /= np.linalg.norm(true_up)
    return position, forward, right, true_up


def generate_rays(
    scene: Dict[str, Any], x0: int, y0: int, x1: int, y1: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    resolution = scene["resolution"]
    width = int(resolution["width"])
    height = int(resolution["height"])
    camera = scene["camera"]
    position, forward, right, true_up = camera_basis(camera)
    fov_y = np.deg2rad(float(camera["fov_y_degrees"]))
    tan_y = float(np.tan(fov_y * 0.5))
    tan_x = tan_y * width / height

    pixel_y, pixel_x = np.mgrid[y0:y1, x0:x1]
    ndc_x = ((pixel_x.astype(np.float64) + 0.5) / width) * 2.0 - 1.0
    ndc_y = 1.0 - ((pixel_y.astype(np.float64) + 0.5) / height) * 2.0
    directions = (
        forward[None, None, :]
        + ndc_x[:, :, None] * tan_x * right[None, None, :]
        + ndc_y[:, :, None] * tan_y * true_up[None, None, :]
    )
    lengths = np.linalg.norm(directions, axis=2, keepdims=True)
    directions = directions / lengths
    origins = np.broadcast_to(position, directions.shape).copy()
    return origins.reshape(-1, 3), directions.reshape(-1, 3), np.asarray((pixel_y, pixel_x))


@dataclass
class RenderResult:
    gray: np.ndarray
    shade: np.ndarray
    depth: np.ndarray
    height: np.ndarray
    normal: np.ndarray
    hit: np.ndarray
    dependency_range: Dict[str, int]
    coverage_range: Dict[str, int]
    sample_buffer_bytes: int


def render_rect(
    scene: Dict[str, Any],
    heightfield: GlobalHeightField,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> RenderResult:
    """Deterministic fixed-step ray march for one screen rectangle."""

    rect_width = x1 - x0
    rect_height = y1 - y0
    pixel_count = rect_width * rect_height
    origins, directions, _ = generate_rays(scene, x0, y0, x1, y1)
    march = scene["ray_march"]
    step_world = float(march["step_world"])
    max_steps = int(march["max_steps"])
    refinement_steps = int(march["refinement_steps"])
    normal_radius = float(march["normal_radius_cells"])
    near = float(scene["camera"]["near"])
    far = float(scene["camera"]["far"])

    tracker = DependencyTracker(heightfield.rows, heightfield.cols)
    hit = np.zeros(pixel_count, dtype=bool)
    hit_t = np.full(pixel_count, np.nan, dtype=np.float64)

    near_points = origins + near * directions
    near_heights = heightfield.sample_world(near_points[:, 0], near_points[:, 2], tracker)
    above = near_points[:, 1] > near_heights
    initial_below = ~above
    if np.any(initial_below):
        hit[initial_below] = True
        hit_t[initial_below] = near

    active_ids = np.flatnonzero(above)
    active_origins = origins[active_ids]
    active_directions = directions[active_ids]
    t_previous = near

    for step_index in range(max_steps):
        if active_ids.size == 0:
            break
        t_current = near + (step_index + 1) * step_world
        if t_current > far:
            break

        current_points = active_origins + t_current * active_directions
        current_heights = heightfield.sample_world(
            current_points[:, 0], current_points[:, 2], tracker
        )
        below_surface = current_points[:, 1] <= current_heights
        crossing_positions = np.flatnonzero(below_surface)

        if crossing_positions.size:
            original_ids = active_ids[crossing_positions]
            crossing_origins = active_origins[crossing_positions]
            crossing_directions = active_directions[crossing_positions]
            low = np.full(crossing_positions.size, t_previous, dtype=np.float64)
            high = np.full(crossing_positions.size, t_current, dtype=np.float64)

            for _ in range(refinement_steps):
                middle = (low + high) * 0.5
                middle_points = crossing_origins + middle[:, None] * crossing_directions
                middle_heights = heightfield.sample_world(
                    middle_points[:, 0], middle_points[:, 2], tracker
                )
                middle_above = middle_points[:, 1] > middle_heights
                low = np.where(middle_above, middle, low)
                high = np.where(middle_above, high, middle)

            refined_t = (low + high) * 0.5
            hit[original_ids] = True
            hit_t[original_ids] = refined_t

        staying_above = ~below_surface
        active_ids = active_ids[staying_above]
        active_origins = active_origins[staying_above]
        active_directions = active_directions[staying_above]
        t_previous = t_current

    hit_ids = np.flatnonzero(hit)
    shade = np.full(pixel_count, 0.06, dtype=np.float64)
    depth = np.full(pixel_count, np.nan, dtype=np.float64)
    surface_height = np.full(pixel_count, np.nan, dtype=np.float64)
    normals = np.tile(np.asarray([0.0, 1.0, 0.0]), (pixel_count, 1))
    coverage_range = {
        "row_start": 0,
        "row_end_exclusive": 0,
        "col_start": 0,
        "col_end_exclusive": 0,
    }

    if hit_ids.size:
        t_values = hit_t[hit_ids]
        hit_origins = origins[hit_ids]
        hit_directions = directions[hit_ids]
        points = hit_origins + t_values[:, None] * hit_directions
        hit_grid_x, hit_grid_z = heightfield.world_to_grid(points[:, 0], points[:, 2])
        hit_col = np.floor(hit_grid_x).astype(np.int64)
        hit_row = np.floor(hit_grid_z).astype(np.int64)
        coverage_range = {
            "row_start": max(0, int(hit_row.min())),
            "row_end_exclusive": min(heightfield.rows, int(hit_row.max()) + 1),
            "col_start": max(0, int(hit_col.min())),
            "col_end_exclusive": min(heightfield.cols, int(hit_col.max()) + 1),
        }
        heights = heightfield.sample_world(points[:, 0], points[:, 2], tracker)
        hit_normals = heightfield.normal_world(
            points[:, 0], points[:, 2], tracker, normal_radius
        )

        light = scene["lighting"]
        light_direction = np.asarray(light["direction"], dtype=np.float64)
        light_direction /= np.linalg.norm(light_direction)
        lambert = np.maximum(hit_normals @ light_direction, 0.0)
        lit = float(light["ambient"]) + float(light["intensity"]) * lambert
        shade[hit_ids] = np.clip(lit, 0.0, 1.0)
        depth[hit_ids] = t_values
        surface_height[hit_ids] = heights
        normals[hit_ids] = hit_normals

    gray_flat = np.clip(np.floor(shade * 255.0 + 0.5), 0.0, 255.0).astype(np.uint8)
    shape = (rect_height, rect_width)
    return RenderResult(
        gray=gray_flat.reshape(shape),
        shade=shade.reshape(shape),
        depth=depth.reshape(shape),
        height=surface_height.reshape(shape),
        normal=normals.reshape(rect_height, rect_width, 3),
        hit=hit.reshape(shape),
        dependency_range=tracker.as_dict(),
        coverage_range=coverage_range,
        sample_buffer_bytes=estimate_sample_buffer_bytes(rect_width, rect_height),
    )


def render_chunk(
    scene: Dict[str, Any], heightfield: GlobalHeightField, chunk: ChunkSpec
) -> RenderResult:
    return render_rect(scene, heightfield, chunk.x0, chunk.y0, chunk.x1, chunk.y1)
