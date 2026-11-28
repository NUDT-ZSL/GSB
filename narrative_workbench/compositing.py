from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .models import CameraFrame, GaussianElement
from .projection import pixel_depth_map, projection_terms

ALPHA_EPSILON = 1.0e-5
PAIR_ALPHA_EPSILON = 0.012
GAUSSIAN_CUTOFF_Q = 9.0


@dataclass
class SampleStack:
    ids: list[str]
    indices: list[int]
    alphas: np.ndarray
    depths: np.ndarray
    colors: np.ndarray
    actual_opacities: np.ndarray

    @property
    def count(self) -> int:
        return len(self.ids)


def _element_actual_opacity(element: GaussianElement, camera: CameraFrame, depth: float) -> tuple[float, float, float]:
    normalized_depth = float(
        np.clip((depth - camera.near) / max(camera.far - camera.near, 1.0e-12), 0.0, 1.0)
    )
    gain = element.depth_curve.sample(normalized_depth)
    actual = float(np.clip(element.opacity * gain, 0.0, 1.0))
    return normalized_depth, gain, actual


def sample_elements(
    elements: list[GaussianElement],
    projected: list[dict[str, Any]],
    camera: CameraFrame,
) -> SampleStack:
    by_id = {element.element_id: element for element in elements}
    index_by_id = {element.element_id: index for index, element in enumerate(elements)}
    height, width = camera.height, camera.width
    count = len(projected)
    alphas = np.zeros((count, height, width), dtype=np.float64)
    depths = np.zeros((count, height, width), dtype=np.float64)
    colors = np.zeros((count, 3), dtype=np.float64)
    actual_opacities = np.zeros(count, dtype=np.float64)
    ids: list[str] = []
    indices: list[int] = []
    tan_x, tan_y = projection_terms(camera)
    for stack_index, record in enumerate(projected):
        element = by_id[record["id"]]
        index = index_by_id[element.element_id]
        ids.append(element.element_id)
        indices.append(index)
        colors[stack_index] = element.color
        centroid_depth = float(record["centroid_depth"])
        _, _, actual = _element_actual_opacity(element, camera, centroid_depth)
        actual_opacities[stack_index] = actual
        x0, y0, x1, y1 = (int(v) for v in record["render_bbox"])
        depth_map = pixel_depth_map(element, camera, x0, y0, x1, y1)
        depths[stack_index, y0 : y1 + 1, x0 : x1 + 1] = depth_map
        screen_covariance = record["screen_covariance"]
        covariance = np.array(
            [
                [screen_covariance[0], screen_covariance[1]],
                [screen_covariance[1], screen_covariance[2]],
            ],
            dtype=np.float64,
        )
        determinant = float(np.linalg.det(covariance))
        if not math.isfinite(determinant) or abs(determinant) < 1.0e-18:
            covariance = covariance + np.eye(2) * 1.0e-12
            determinant = float(np.linalg.det(covariance))
        inverse = np.array(
            [
                [covariance[1, 1], -covariance[0, 1]],
                [-covariance[0, 1], covariance[0, 0]],
            ],
            dtype=np.float64,
        ) / determinant
        xs = np.arange(x0, x1 + 1, dtype=np.float64) + 0.5
        ys = np.arange(y0, y1 + 1, dtype=np.float64) + 0.5
        dx = xs[None, :] - float(record["screen_center"][0])
        dy = ys[:, None] - float(record["screen_center"][1])
        q = (
            inverse[0, 0] * dx * dx
            + 2.0 * inverse[0, 1] * dx * dy
            + inverse[1, 1] * dy * dy
        )
        local_alpha = np.where(
            q <= GAUSSIAN_CUTOFF_Q,
            actual * np.exp(-0.5 * np.minimum(q, 80.0)),
            0.0,
        )
        local_alpha = np.where(local_alpha >= ALPHA_EPSILON, local_alpha, 0.0)
        alphas[stack_index, y0 : y1 + 1, x0 : x1 + 1] = local_alpha
    return SampleStack(
        ids=ids,
        indices=indices,
        alphas=alphas,
        depths=depths,
        colors=colors,
        actual_opacities=actual_opacities,
    )


def composite_in_order(
    order: list[int],
    alphas: np.ndarray,
    colors: np.ndarray,
) -> np.ndarray:
    height, width = alphas.shape[1:]
    result = np.zeros((height, width, 4), dtype=np.float64)
    for stack_index in order:
        source_alpha = alphas[stack_index]
        previous_alpha = result[..., 3]
        next_alpha = source_alpha + previous_alpha * (1.0 - source_alpha)
        numerator = (
            colors[stack_index][None, None, :] * source_alpha[..., None]
            + result[..., :3] * previous_alpha[..., None] * (1.0 - source_alpha[..., None])
        )
        result[..., :3] = np.where(
            next_alpha[..., None] > 1.0e-15,
            numerator / np.maximum(next_alpha[..., None], 1.0e-15),
            0.0,
        )
        result[..., 3] = next_alpha
    return result


def composite_pixel_sorted(
    indices: list[int],
    alphas: np.ndarray,
    depths: np.ndarray,
    colors: np.ndarray,
) -> np.ndarray:
    height, width = alphas.shape[1:]
    result = np.zeros((height, width, 4), dtype=np.float64)
    if not indices:
        return result
    depth_subset = depths[indices]
    depth_for_sort = np.where(
        alphas[indices] >= ALPHA_EPSILON,
        depth_subset,
        -np.inf,
    )
    order = np.argsort(-depth_for_sort, axis=0, kind="stable")
    subset_alphas = alphas[indices]
    for rank in range(len(indices)):
        selected = order[rank]
        source_alpha = np.take_along_axis(
            subset_alphas, selected[None, :, :], axis=0
        )[0]
        source_color = colors[indices][selected]
        previous_alpha = result[..., 3]
        next_alpha = source_alpha + previous_alpha * (1.0 - source_alpha)
        numerator = (
            source_color * source_alpha[..., None]
            + result[..., :3] * previous_alpha[..., None] * (1.0 - source_alpha[..., None])
        )
        result[..., :3] = np.where(
            next_alpha[..., None] > 1.0e-15,
            numerator / np.maximum(next_alpha[..., None], 1.0e-15),
            0.0,
        )
        result[..., 3] = next_alpha
    return result


def composite_layer_approximation(
    elements: list[GaussianElement],
    stacked_indices: list[int],
    samples: SampleStack,
    projected: list[dict[str, Any]],
) -> tuple[np.ndarray, list[int]]:
    height, width = samples.alphas.shape[1:]
    result = np.zeros((height, width, 4), dtype=np.float64)
    groups: dict[int, list[int]] = {}
    for stack_index in stacked_indices:
        layer_index = elements[samples.indices[stack_index]].layer_index
        groups.setdefault(layer_index, []).append(stack_index)
    layer_order = sorted(
        groups,
        key=lambda layer_index: (
            -float(
                np.mean(
                    [projected[index]["centroid_depth"] for index in groups[layer_index]]
                )
            ),
            layer_index,
        ),
    )
    for layer_index in layer_order:
        layer_indices = groups[layer_index]
        layer_rgba = composite_pixel_sorted(
            layer_indices,
            samples.alphas,
            samples.depths,
            samples.colors,
        )
        layer_alpha = layer_rgba[..., 3]
        previous_alpha = result[..., 3]
        next_alpha = layer_alpha + previous_alpha * (1.0 - layer_alpha)
        numerator = (
            layer_rgba[..., :3] * layer_alpha[..., None]
            + result[..., :3] * previous_alpha[..., None] * (1.0 - layer_alpha[..., None])
        )
        result[..., :3] = np.where(
            next_alpha[..., None] > 1.0e-15,
            numerator / np.maximum(next_alpha[..., None], 1.0e-15),
            0.0,
        )
        result[..., 3] = next_alpha
    return result, layer_order
