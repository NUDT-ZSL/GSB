"""Deterministic texture baking with a block/full parity contract."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from .errors import TextureBlockMismatch
from .models import CameraPose


@dataclass(frozen=True)
class TexturedPath:
    full_texture: np.ndarray
    block_texture: np.ndarray
    weight_sums: np.ndarray
    visible_camera_counts: np.ndarray
    base_colors: np.ndarray
    comparison: dict[str, Any]
    multi_view: dict[str, Any]
    blocks: tuple[dict[str, int], ...]

    @property
    def texture_scale(self) -> float:
        return max(
            float(np.max(np.abs(self.full_texture))),
            float(np.finfo(np.float64).tiny),
        )


def _base_color(texture_seed: int, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    phase = float(int(texture_seed) % 104729) / 104729.0
    red = (
        0.50
        + 0.23 * np.sin(2.0 * np.pi * (1.2 * u + 0.7 * v + phase))
        + 0.08 * np.cos(2.0 * np.pi * (2.1 * v - phase))
    )
    green = (
        0.47
        + 0.20 * np.cos(2.0 * np.pi * (1.6 * v + 0.4 * u + 0.6 * phase))
        + 0.07 * np.sin(2.0 * np.pi * (2.4 * u + phase))
    )
    blue = (
        0.53
        + 0.18 * np.sin(2.0 * np.pi * (0.8 * (u + v) - 0.3 * phase))
        + 0.11 * np.cos(2.0 * np.pi * (1.4 * u - 0.9 * v + phase))
    )
    return np.stack(
        (
            np.clip(red, 0.0, 1.0),
            np.clip(green, 0.0, 1.0),
            np.clip(blue, 0.0, 1.0),
        ),
        axis=2,
    ).astype(np.float64, copy=False)


def _rasterize_uv(
    vertices: np.ndarray,
    faces: np.ndarray,
    uv: np.ndarray,
    height: int,
    width: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    yy, xx = np.meshgrid(
        np.arange(height, dtype=np.float64),
        np.arange(width, dtype=np.float64),
        indexing="ij",
    )
    pixel_u = (xx + 0.5) / float(width)
    pixel_v = (yy + 0.5) / float(height)
    points = np.zeros((height, width, 3), dtype=np.float64)
    normals = np.zeros((height, width, 3), dtype=np.float64)
    valid = np.zeros((height, width), dtype=np.bool_)
    epsilon = 1e-13

    for face_index in range(faces.shape[0]):
        face = faces[face_index]
        a = vertices[int(face[0])]
        b = vertices[int(face[1])]
        c = vertices[int(face[2])]
        normal = np.cross(b - a, c - a)
        normal_length = float(np.linalg.norm(normal))
        if normal_length <= epsilon:
            continue
        normal = normal / normal_length
        ua = uv[int(face[0])]
        ub = uv[int(face[1])]
        uc = uv[int(face[2])]
        denominator = (ub[1] - uc[1]) * (ua[0] - uc[0]) + (
            uc[0] - ub[0]
        ) * (ua[1] - uc[1])
        if abs(float(denominator)) <= epsilon:
            continue
        w0 = (
            (ub[1] - uc[1]) * (pixel_u - uc[0])
            + (uc[0] - ub[0]) * (pixel_v - uc[1])
        ) / denominator
        w1 = (
            (uc[1] - ua[1]) * (pixel_u - uc[0])
            + (ua[0] - uc[0]) * (pixel_v - uc[1])
        ) / denominator
        w2 = 1.0 - w0 - w1
        inside = (
            (w0 >= -epsilon)
            & (w1 >= -epsilon)
            & (w2 >= -epsilon)
            & (~valid)
        )
        if not np.any(inside):
            continue
        points[inside] = (
            w0[inside, None] * a
            + w1[inside, None] * b
            + w2[inside, None] * c
        )
        normals[inside] = normal
        valid[inside] = True
    return points, normals, valid


def _bilinear_sample(image: np.ndarray, x: float, y: float) -> np.ndarray:
    height, width = image.shape[:2]
    x_clamped = min(max(float(x), 0.0), float(width - 1))
    y_clamped = min(max(float(y), 0.0), float(height - 1))
    x0 = int(math.floor(x_clamped))
    y0 = int(math.floor(y_clamped))
    x1 = min(x0 + 1, width - 1)
    y1 = min(y0 + 1, height - 1)
    tx = np.float64(x_clamped - x0)
    ty = np.float64(y_clamped - y0)
    top = (np.float64(1.0) - tx) * image[y0, x0] + tx * image[y0, x1]
    bottom = (np.float64(1.0) - tx) * image[y1, x0] + tx * image[y1, x1]
    return (np.float64(1.0) - ty) * top + ty * bottom


def _build_camera_contributions(
    points: np.ndarray,
    normals: np.ndarray,
    valid: np.ndarray,
    cameras: Sequence[CameraPose],
    camera_images: Mapping[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    height, width = valid.shape
    camera_count = len(cameras)
    colors = np.zeros((height, width, camera_count, 3), dtype=np.float64)
    weights = np.zeros((height, width, camera_count), dtype=np.float64)
    epsilon = 1e-13

    for camera_index, camera in enumerate(cameras):
        image = np.asarray(camera_images[camera.camera_id], dtype=np.float64)
        rotation = camera.rotation
        translation = camera.translation
        intrinsics = camera.intrinsics
        fx = float(intrinsics[0, 0])
        fy = float(intrinsics[1, 1])
        cx = float(intrinsics[0, 2])
        cy = float(intrinsics[1, 2])
        image_height, image_width = image.shape[:2]
        camera_position = translation
        for row in range(height):
            for col in range(width):
                if not bool(valid[row, col]):
                    continue
                point = points[row, col]
                camera_point = (point - camera_position) @ rotation
                z_value = float(camera_point[2])
                if z_value <= epsilon:
                    continue
                x_value = fx * float(camera_point[0]) / z_value + cx
                y_value = fy * float(camera_point[1]) / z_value + cy
                if (
                    x_value < 0.0
                    or x_value > float(image_width - 1)
                    or y_value < 0.0
                    or y_value > float(image_height - 1)
                ):
                    continue
                normal = normals[row, col]
                view = camera_position - point
                view_length = float(np.linalg.norm(view))
                if view_length <= epsilon:
                    continue
                facing = abs(float(np.dot(normal, view / view_length)))
                weight = max(0.02, facing) / (1.0 + 0.15 * z_value)
                colors[row, col, camera_index] = _bilinear_sample(
                    image, x_value, y_value
                )
                weights[row, col, camera_index] = weight
    return colors, weights


def _integrate_pixel(
    row: int,
    col: int,
    contributions: np.ndarray,
    weights: np.ndarray,
    base_colors: np.ndarray,
) -> np.ndarray:
    camera_count = weights.shape[2]
    weight_sum = np.float64(0.0)
    color_sum = np.zeros(3, dtype=np.float64)
    for camera_index in range(camera_count):
        weight = np.float64(weights[row, col, camera_index])
        if weight == 0.0:
            continue
        weight_sum = weight_sum + weight
        color_sum = color_sum + weight * contributions[row, col, camera_index]
    if weight_sum > 0.0:
        return color_sum / weight_sum
    return base_colors[row, col].copy()


def _integrate_full(
    contributions: np.ndarray,
    weights: np.ndarray,
    base_colors: np.ndarray,
) -> np.ndarray:
    height, width = weights.shape[:2]
    texture = np.empty((height, width, 3), dtype=np.float64)
    for row in range(height):
        for col in range(width):
            texture[row, col] = _integrate_pixel(
                row, col, contributions, weights, base_colors
            )
    return texture


def _block_ranges(length: int, block: int) -> list[tuple[int, int]]:
    return [(start, min(start + block, length)) for start in range(0, length, block)]


def _integrate_blocks(
    contributions: np.ndarray,
    weights: np.ndarray,
    base_colors: np.ndarray,
    block_height: int,
    block_width: int,
) -> tuple[np.ndarray, tuple[dict[str, int], ...]]:
    height, width = weights.shape[:2]
    texture = np.empty((height, width, 3), dtype=np.float64)
    blocks: list[dict[str, int]] = []
    for row_start, row_stop in _block_ranges(height, block_height):
        for col_start, col_stop in _block_ranges(width, block_width):
            blocks.append(
                {
                    "row_start": int(row_start),
                    "row_stop": int(row_stop),
                    "col_start": int(col_start),
                    "col_stop": int(col_stop),
                }
            )
            for row in range(row_start, row_stop):
                for col in range(col_start, col_stop):
                    texture[row, col] = _integrate_pixel(
                        row, col, contributions, weights, base_colors
                    )
    return texture, tuple(blocks)


def _deviation_summary(difference: np.ndarray) -> dict[str, Any]:
    if difference.size == 0:
        values = np.empty(0, dtype=np.float64)
    else:
        values = np.max(np.abs(difference), axis=2).reshape(-1)
    if values.size == 0:
        return {
            "metric": "max_abs_rgb_per_pixel",
            "count": 0,
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p99": 0.0,
            "histogram": {"bin_edges": [0.0, 0.0], "counts": [0]},
        }
    maximum = float(np.max(values))
    if maximum > 0.0:
        bin_edges = np.linspace(0.0, maximum, num=17, dtype=np.float64)
        counts, edges = np.histogram(values, bins=bin_edges)
    else:
        counts = np.zeros(16, dtype=np.int64)
        edges = np.linspace(0.0, 1.0, num=17, dtype=np.float64)
    return {
        "metric": "max_abs_rgb_per_pixel",
        "count": int(values.size),
        "min": float(np.min(values)),
        "max": maximum,
        "mean": float(np.mean(values)),
        "p50": float(np.percentile(values, 50)),
        "p90": float(np.percentile(values, 90)),
        "p99": float(np.percentile(values, 99)),
        "histogram": {
            "bin_edges": [float(item) for item in edges],
            "counts": [int(item) for item in counts],
        },
    }


def compare_texture_paths(
    full_texture: np.ndarray,
    block_texture: np.ndarray,
    *,
    scene_id: str | None = None,
) -> dict[str, Any]:
    full = np.asarray(full_texture, dtype=np.float64)
    block = np.asarray(block_texture, dtype=np.float64)
    if full.shape != block.shape:
        first_pixel = (0, 0)
        difference = np.ones((1, 1, 3), dtype=np.float64)
        spectrum = _deviation_summary(difference)
        spectrum["shape_mismatch"] = {"full": list(full.shape), "block": list(block.shape)}
        raise TextureBlockMismatch(
            first_pixel=first_pixel,
            deviation_spectrum=spectrum,
            scene_id=scene_id,
        )
    if full.tobytes(order="C") == block.tobytes(order="C"):
        difference = np.zeros_like(full)
        return {
            "exact_equal": True,
            "max_abs_diff": 0.0,
            "first_mismatch": None,
            "deviation_spectrum": _deviation_summary(difference),
        }

    difference = block - full
    per_pixel = np.max(np.abs(difference), axis=2)
    mismatch_positions = np.argwhere(per_pixel > 0.0)
    if mismatch_positions.size == 0:
        mismatch_positions = np.argwhere(np.abs(difference) > 0.0)
    if mismatch_positions.size:
        first = mismatch_positions[0]
        first_pixel = (int(first[0]), int(first[1]))
    else:
        first_pixel = (0, 0)
    spectrum = _deviation_summary(difference)
    spectrum["first_pixel_abs_rgb"] = [
        float(item) for item in np.abs(difference[first_pixel[0], first_pixel[1]])
    ]
    raise TextureBlockMismatch(
        first_pixel=first_pixel,
        deviation_spectrum=spectrum,
        scene_id=scene_id,
    )


def _multi_view_consistency(
    contributions: np.ndarray,
    weights: np.ndarray,
    tolerance: float,
) -> dict[str, Any]:
    height, width, camera_count = weights.shape
    spreads: list[float] = []
    for row in range(height):
        for col in range(width):
            visible = np.flatnonzero(weights[row, col] > 0.0)
            if visible.size < 2:
                continue
            selected_weights = weights[row, col, visible]
            selected_colors = contributions[row, col, visible]
            weight_sum = np.float64(0.0)
            for index in range(visible.size):
                weight_sum = weight_sum + selected_weights[index]
            if weight_sum <= 0.0:
                continue
            mean = np.zeros(3, dtype=np.float64)
            for index in range(visible.size):
                mean = mean + selected_weights[index] * selected_colors[index]
            mean = mean / weight_sum
            variance = np.zeros(3, dtype=np.float64)
            for index in range(visible.size):
                delta = selected_colors[index] - mean
                variance = variance + selected_weights[index] * delta * delta
            variance = variance / weight_sum
            spreads.append(float(np.sqrt(np.max(np.maximum(variance, 0.0)))))
    if spreads:
        values = np.asarray(spreads, dtype=np.float64)
        max_spread = float(np.max(values))
        mean_spread = float(np.mean(values))
        p95_spread = float(np.percentile(values, 95))
    else:
        max_spread = 0.0
        mean_spread = 0.0
        p95_spread = 0.0
    return {
        "multi_view_pixels": len(spreads),
        "mean_spread": mean_spread,
        "p95_spread": p95_spread,
        "max_spread": max_spread,
        "tolerance": float(tolerance),
        "consistent": bool(max_spread <= float(tolerance)),
        "camera_count": int(camera_count),
    }


def bake_texture_paths(
    vertices: np.ndarray,
    faces: np.ndarray,
    uv: np.ndarray,
    cameras: Sequence[CameraPose],
    camera_images: Mapping[str, np.ndarray],
    *,
    texture_seed: int,
    height: int,
    width: int,
    block_height: int,
    block_width: int,
    multi_view_tolerance: float,
    scene_id: str | None = None,
) -> TexturedPath:
    mesh_vertices = np.asarray(vertices, dtype=np.float64)
    mesh_faces = np.asarray(faces, dtype=np.int64)
    mesh_uv = np.asarray(uv, dtype=np.float64)
    sorted_cameras = tuple(sorted(cameras, key=lambda camera: camera.camera_id))
    if sorted_cameras and any(
        camera.camera_id not in camera_images for camera in sorted_cameras
    ):
        raise ValueError("missing camera image for one or more cameras")

    surface_points, surface_normals, valid = _rasterize_uv(
        mesh_vertices, mesh_faces, mesh_uv, int(height), int(width)
    )
    yy, xx = np.meshgrid(
        np.arange(height, dtype=np.float64),
        np.arange(width, dtype=np.float64),
        indexing="ij",
    )
    base_colors = _base_color(
        int(texture_seed),
        (xx + 0.5) / float(width),
        (yy + 0.5) / float(height),
    )
    if sorted_cameras:
        contributions, weights = _build_camera_contributions(
            surface_points,
            surface_normals,
            valid,
            sorted_cameras,
            camera_images,
        )
    else:
        contributions = np.zeros((height, width, 0, 3), dtype=np.float64)
        weights = np.zeros((height, width, 0), dtype=np.float64)

    full_texture = _integrate_full(contributions, weights, base_colors)
    block_texture, blocks = _integrate_blocks(
        contributions,
        weights,
        base_colors,
        int(block_height),
        int(block_width),
    )
    comparison = compare_texture_paths(
        full_texture, block_texture, scene_id=scene_id
    )
    weight_sums = np.sum(weights, axis=2, dtype=np.float64)
    visible_camera_counts = np.count_nonzero(weights > 0.0, axis=2).astype(np.int64)
    multi_view = _multi_view_consistency(
        contributions, weights, float(multi_view_tolerance)
    )
    return TexturedPath(
        full_texture=np.ascontiguousarray(full_texture, dtype=np.float64),
        block_texture=np.ascontiguousarray(block_texture, dtype=np.float64),
        weight_sums=np.ascontiguousarray(weight_sums, dtype=np.float64),
        visible_camera_counts=np.ascontiguousarray(
            visible_camera_counts, dtype=np.int64
        ),
        base_colors=np.ascontiguousarray(base_colors, dtype=np.float64),
        comparison=comparison,
        multi_view=multi_view,
        blocks=blocks,
    )

