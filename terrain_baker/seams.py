from __future__ import annotations

from typing import Any, Dict, List, Sequence

import numpy as np

from .graph import ChunkSpec


def assemble_grid(
    chunks: Sequence[ChunkSpec],
    arrays_by_id: Dict[str, Dict[str, np.ndarray]],
    shape: tuple[int, int],
) -> Dict[str, np.ndarray]:
    height, width = shape
    gray = np.zeros((height, width), dtype=np.uint8)
    shade = np.zeros((height, width), dtype=np.float64)
    depth = np.full((height, width), np.nan, dtype=np.float64)
    surface_height = np.full((height, width), np.nan, dtype=np.float64)
    normal = np.zeros((height, width, 3), dtype=np.float64)
    hit = np.zeros((height, width), dtype=bool)

    for chunk in chunks:
        arrays = arrays_by_id[chunk.chunk_id]
        gray[chunk.y0 : chunk.y1, chunk.x0 : chunk.x1] = arrays["gray"]
        shade[chunk.y0 : chunk.y1, chunk.x0 : chunk.x1] = arrays["shade"]
        depth[chunk.y0 : chunk.y1, chunk.x0 : chunk.x1] = arrays["depth"]
        surface_height[chunk.y0 : chunk.y1, chunk.x0 : chunk.x1] = arrays["height"]
        normal[chunk.y0 : chunk.y1, chunk.x0 : chunk.x1, :] = arrays["normal"]
        hit[chunk.y0 : chunk.y1, chunk.x0 : chunk.x1] = arrays["hit"]
    return {
        "gray": gray,
        "shade": shade,
        "depth": depth,
        "height": surface_height,
        "normal": normal,
        "hit": hit,
    }


def boundary_band_mask(chunks: Sequence[ChunkSpec], height: int, width: int) -> np.ndarray:
    mask = np.zeros((height, width), dtype=bool)
    for chunk in chunks:
        if chunk.tx > 0:
            x = chunk.x0
            mask[chunk.y0 : chunk.y1, max(0, x - 1) : min(width, x + 1)] = True
        if chunk.ty > 0:
            y = chunk.y0
            mask[max(0, y - 1) : min(height, y + 1), chunk.x0 : chunk.x1] = True
    return mask


def adjacent_boundary_normal_angle_max(
    normal: np.ndarray, chunks: Sequence[ChunkSpec]
) -> float:
    angles = [0.0]
    height, width, _ = normal.shape
    for chunk in chunks:
        if chunk.tx > 0 and 0 < chunk.x0 < width:
            left = normal[chunk.y0 : chunk.y1, chunk.x0 - 1, :]
            right = normal[chunk.y0 : chunk.y1, chunk.x0, :]
            dots = np.sum(left * right, axis=1)
            dots = np.clip(dots, -1.0, 1.0)
            angles.extend(np.rad2deg(np.arccos(dots)).tolist())
        if chunk.ty > 0 and 0 < chunk.y0 < height:
            top = normal[chunk.y0 - 1, chunk.x0 : chunk.x1, :]
            bottom = normal[chunk.y0, chunk.x0 : chunk.x1, :]
            dots = np.sum(top * bottom, axis=1)
            dots = np.clip(dots, -1.0, 1.0)
            angles.extend(np.rad2deg(np.arccos(dots)).tolist())
    return float(max(angles))


def max_numeric_diff(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return float("inf")
    if np.issubdtype(a.dtype, np.bool_) or np.issubdtype(b.dtype, np.bool_):
        return float(np.max(a != b)) if a.size else 0.0
    aa = np.asarray(a, dtype=np.float64)
    bb = np.asarray(b, dtype=np.float64)
    finite = np.isfinite(aa) & np.isfinite(bb)
    nonfinite_mismatch = bool(np.any(np.isfinite(aa) != np.isfinite(bb)))
    max_diff = float(np.max(np.abs(aa[finite] - bb[finite]))) if np.any(finite) else 0.0
    if nonfinite_mismatch:
        return float("inf")
    return max_diff


def compare_to_reference(
    assembled: Dict[str, np.ndarray],
    reference: Any,
    chunks: Sequence[ChunkSpec],
) -> Dict[str, Any]:
    height, width = assembled["gray"].shape
    boundary_mask = boundary_band_mask(chunks, height, width)
    boundary_normal_diff = np.linalg.norm(
        assembled["normal"][boundary_mask] - reference.normal[boundary_mask], axis=1
    )
    assembled_boundary_normals = assembled["normal"][boundary_mask]
    reference_boundary_normals = reference.normal[boundary_mask]
    boundary_dots = np.sum(
        assembled_boundary_normals * reference_boundary_normals, axis=1
    )
    boundary_angle_diff = np.rad2deg(np.arccos(np.clip(boundary_dots, -1.0, 1.0)))
    boundary_angle_diff[boundary_normal_diff == 0.0] = 0.0
    return {
        "gray_pixel_max_abs_diff": int(
            np.max(np.abs(assembled["gray"].astype(np.int16) - reference.gray.astype(np.int16)))
        ),
        "shade_float_max_abs_diff": max_numeric_diff(assembled["shade"], reference.shade),
        "depth_world_max_abs_diff": max_numeric_diff(assembled["depth"], reference.depth),
        "height_world_max_abs_diff": max_numeric_diff(assembled["height"], reference.height),
        "normal_vector_max_abs_diff": max_numeric_diff(assembled["normal"], reference.normal),
        "boundary_band_normal_vector_max_diff_vs_full": float(np.max(boundary_normal_diff)),
        "boundary_band_normal_angle_max_diff_vs_full_degrees": float(
            np.max(boundary_angle_diff) if boundary_angle_diff.size else 0.0
        ),
        "screen_boundary_adjacent_normal_angle_max_degrees": adjacent_boundary_normal_angle_max(
            assembled["normal"], chunks
        ),
        "boundary_pixel_count": int(np.count_nonzero(boundary_mask)),
        "pixel_exact": bool(
            np.array_equal(assembled["gray"], reference.gray)
            and np.array_equal(assembled["depth"], reference.depth, equal_nan=True)
            and np.array_equal(assembled["height"], reference.height, equal_nan=True)
        ),
    }
