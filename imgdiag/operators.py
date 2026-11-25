from __future__ import annotations

from typing import Any

import numpy as np

from .numeric import PrecisionContext


def weighted_color_sum(weights: np.ndarray, colors: np.ndarray) -> np.ndarray:
    """Explicit K-accumulation; order is fixed for full and block slices."""
    count = weights.shape[1]
    result = np.zeros((weights.shape[0], 3), dtype=np.float64)
    for k in range(count):
        result += weights[:, k:k + 1].astype(np.float64) * colors[:, k, :].astype(np.float64)
    return result


def _nearest_source_indices(distances: np.ndarray, k: int) -> np.ndarray:
    order = np.argsort(distances, axis=1, kind="stable")
    return order[:, :k]


def build_region_plan(
    region: dict[str, Any],
    boundary_neighbors: int = 8,
    feather: float = 2.0,
    epsilon: float = 1e-12,
    pc: PrecisionContext | None = None,
) -> dict[str, Any]:
    pc = pc or PrecisionContext()
    coords = np.asarray(region["pixel_coords"], dtype=np.int64)
    context_coords = np.asarray(region["context_coords"], dtype=np.int64)
    context_rgb = np.asarray(region["context_rgb"], dtype=np.float64)
    if context_rgb.size == 0:
        raise ValueError("region has no context RGB samples")
    context_rgb = pc.q(context_rgb)

    pixel_count = coords.shape[0]
    context_count = context_coords.shape[0]
    k = max(1, min(int(boundary_neighbors), context_count))

    coord_delta = coords[:, None, :] - context_coords[None, :, :]
    distances = np.sqrt(np.sum(coord_delta.astype(np.float64) ** 2, axis=2))
    source_idx = _nearest_source_indices(distances, k)
    source_dist = np.take_along_axis(distances, source_idx, axis=1)
    raw_weights = 1.0 / (source_dist ** 2 + 1e-6)
    weights = raw_weights / np.sum(raw_weights, axis=1, keepdims=True)
    weights = pc.q(weights)
    weights = weights / np.sum(weights.astype(np.float64), axis=1, keepdims=True)

    source_colors = context_rgb[source_idx]
    sampled = weighted_color_sum(weights, source_colors)

    # Leave-one-context-sample-out interpolation gives a deterministic boundary
    # residual field.  Pixel weights reuse the exact same inverse-distance rule.
    residual = np.zeros_like(context_rgb, dtype=np.float64)
    if context_count >= 3:
        ctx_delta = context_coords[:, None, :] - context_coords[None, :, :]
        ctx_dist = np.sqrt(np.sum(ctx_delta.astype(np.float64) ** 2, axis=2))
        np.fill_diagonal(ctx_dist, np.inf)
        residual_k = max(1, min(k, context_count - 1))
        residual_idx = _nearest_source_indices(ctx_dist, residual_k)
        residual_dist = np.take_along_axis(ctx_dist, residual_idx, axis=1)
        residual_raw = 1.0 / (residual_dist ** 2 + 1e-6)
        residual_weights = residual_raw / np.sum(residual_raw, axis=1, keepdims=True)
        residual_colors = context_rgb[residual_idx]
        residual_estimate = weighted_color_sum(residual_weights, residual_colors)
        residual = np.asarray(context_rgb, dtype=np.float64) - residual_estimate
        residual = pc.q(residual)

    source_residual = residual[source_idx]
    residual_field = weighted_color_sum(weights, source_residual)

    mean = np.sum(context_rgb.astype(np.float64), axis=0) / float(context_count)
    variance = np.sum((context_rgb.astype(np.float64) - mean) ** 2, axis=0) / float(context_count)
    scale = np.sqrt(variance + float(epsilon))
    mean = pc.q(mean)
    scale = pc.q(scale)

    nearest_context_distance = np.min(distances, axis=1)
    alpha = np.clip(nearest_context_distance / max(float(feather), 1e-6), 0.0, 1.0)
    alpha = pc.q(alpha)

    return {
        "region_id": region["region_id"],
        "coords": coords,
        "context_coords": context_coords,
        "context_rgb": context_rgb,
        "source_idx": source_idx,
        "source_coords": context_coords[source_idx],
        "weights": weights,
        "source_colors": source_colors,
        "sampled": sampled,
        "residual": residual,
        "residual_field": residual_field,
        "mean": mean,
        "scale": scale,
        "alpha": alpha,
        "pixel_count": pixel_count,
        "context_count": context_count,
        "stats_digest_inputs": {
            "mean": np.asarray(mean, dtype=np.float64).tolist(),
            "scale": np.asarray(scale, dtype=np.float64).tolist(),
            "k": k,
        },
    }


def subset_plan(plan: dict[str, Any], indices: np.ndarray) -> dict[str, Any]:
    """Select pixel rows while retaining all global stats and coefficients."""
    subset = dict(plan)
    for key in (
        "coords", "source_idx", "source_coords", "weights", "source_colors",
        "sampled", "residual_field", "alpha",
    ):
        subset[key] = np.asarray(plan[key])[indices]
    # residual is context-indexed and must remain global.
    subset["residual"] = plan["residual"]
    return subset


def completion_kernel(
    plan: dict[str, Any],
    iterations: int,
    pc: PrecisionContext,
) -> tuple[np.ndarray, np.ndarray]:
    nearest = np.asarray(plan["source_colors"][:, 0, :], dtype=pc.numpy_dtype)
    base = np.asarray(plan["sampled"], dtype=pc.numpy_dtype)
    residual_field = np.asarray(plan["residual_field"], dtype=pc.numpy_dtype)
    target = base + np.asarray(0.25, dtype=pc.numpy_dtype) * residual_field
    completed = nearest.copy()
    step = np.asarray(0.5, dtype=pc.numpy_dtype)
    for _ in range(max(1, int(iterations))):
        completed = completed + step * (target - completed)
        completed = pc.q(completed)
    contributions = np.asarray(plan["weights"])[:, :, None] * np.asarray(plan["source_colors"])
    return completed, contributions


def style_kernel(
    completed: np.ndarray,
    plan: dict[str, Any],
    matrix: np.ndarray,
    style_strength: float,
    pc: PrecisionContext,
) -> tuple[np.ndarray, np.ndarray]:
    completed_cast = np.asarray(completed, dtype=pc.numpy_dtype)
    mean = np.asarray(plan["mean"], dtype=pc.numpy_dtype)
    scale = np.asarray(plan["scale"], dtype=pc.numpy_dtype)
    matrix_cast = pc.transform_matrix(matrix)
    normalized = (completed_cast - mean) / scale

    n0 = normalized[:, 0]
    n1 = normalized[:, 1]
    n2 = normalized[:, 2]
    m = matrix_cast
    out0 = n0 * m[0, 0] + n1 * m[0, 1] + n2 * m[0, 2]
    out1 = n0 * m[1, 0] + n1 * m[1, 1] + n2 * m[1, 2]
    out2 = n0 * m[2, 0] + n1 * m[2, 1] + n2 * m[2, 2]
    migrated = np.stack([out0, out1, out2], axis=1)
    restored = migrated * scale + mean
    strength = np.asarray(float(style_strength), dtype=pc.numpy_dtype)
    styled = completed_cast * (np.asarray(1.0, dtype=pc.numpy_dtype) - strength) + restored * strength
    styled = pc.q(styled)

    contributions = np.stack([
        np.stack([n0 * m[0, 0], n1 * m[0, 1], n2 * m[0, 2]], axis=1),
        np.stack([n0 * m[1, 0], n1 * m[1, 1], n2 * m[1, 2]], axis=1),
        np.stack([n0 * m[2, 0], n1 * m[2, 1], n2 * m[2, 2]], axis=1),
    ], axis=1)
    return np.asarray(styled, dtype=np.float64), np.asarray(contributions, dtype=np.float64)


def fusion_kernel(
    image: np.ndarray,
    coords: np.ndarray,
    styled: np.ndarray,
    alpha: np.ndarray,
    pc: PrecisionContext,
) -> np.ndarray:
    original = image[coords[:, 0], coords[:, 1]].astype(np.float64)
    styled64 = np.asarray(styled, dtype=np.float64)
    alpha64 = np.asarray(alpha, dtype=np.float64)[:, None]
    fused = original * (1.0 - alpha64) + styled64 * alpha64
    fused = pc.q(fused)
    return np.asarray(fused, dtype=np.float64)
