"""Deterministic feature extraction for synthetic chromatic texture markers."""

from __future__ import annotations

from typing import Any

import numpy as np

from .canonical import canonical_float
from .io_views import ViewInput


def _gaussian_kernel() -> np.ndarray:
    values = np.array([1.0, 4.0, 6.0, 4.0, 1.0], dtype=np.float64)
    kernel = np.outer(values, values)
    return kernel / kernel.sum()


_KERNEL = _gaussian_kernel()


def _convolve_same(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    radius_y = kernel.shape[0] // 2
    radius_x = kernel.shape[1] // 2
    padded = np.pad(image, ((radius_y, radius_y), (radius_x, radius_x)), mode="reflect")
    result = np.zeros_like(image, dtype=np.float64)
    for kernel_y in range(kernel.shape[0]):
        for kernel_x in range(kernel.shape[1]):
            weight = kernel[kernel_y, kernel_x]
            if weight == 0.0:
                continue
            result += weight * padded[
                kernel_y : kernel_y + image.shape[0],
                kernel_x : kernel_x + image.shape[1],
            ]
    return result


def _local_max_mask(image: np.ndarray, radius: int) -> np.ndarray:
    height, width = image.shape
    maximum = np.full_like(image, -np.inf)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            source_y0 = max(0, -dy)
            source_y1 = min(height, height - dy)
            source_x0 = max(0, -dx)
            source_x1 = min(width, width - dx)
            if source_y0 >= source_y1 or source_x0 >= source_x1:
                continue
            target_y0 = source_y0 + dy
            target_y1 = source_y1 + dy
            target_x0 = source_x0 + dx
            target_x1 = source_x1 + dx
            maximum[target_y0:target_y1, target_x0:target_x1] = np.maximum(
                maximum[target_y0:target_y1, target_x0:target_x1],
                image[source_y0:source_y1, source_x0:source_x1],
            )
    return image >= maximum


def _patch_descriptor(
    color: np.ndarray,
    center_y: int,
    center_x: int,
    radius: int,
) -> list[float]:
    # 5x5 overlapping 3x3 means from each RGB channel. Block means are stable
    # under the one-pixel projection jitter seen in adjacent synthetic views.
    offsets = np.linspace(-radius, radius, 5)
    rounded_offsets = sorted(set(int(round(value)) for value in offsets))
    if len(rounded_offsets) != 5:
        rounded_offsets = [-radius, -radius // 2, 0, radius // 2, radius]
    descriptor: list[float] = []
    height, width, _ = color.shape
    for offset_y in rounded_offsets:
        for offset_x in rounded_offsets:
            y = center_y + offset_y
            x = center_x + offset_x
            y0 = max(0, y - 1)
            y1 = min(height, y + 2)
            x0 = max(0, x - 1)
            x1 = min(width, x + 2)
            block = color[y0:y1, x0:x1, :]
            means = block.mean(axis=(0, 1)) / 255.0
            descriptor.extend(canonical_float(value, 8) for value in means.tolist())
    return descriptor


def extract_features(view: ViewInput, feature_params: dict[str, Any], seed: int) -> dict[str, Any]:
    del seed  # The deterministic detector intentionally requires no random draw.
    color = np.asarray(view.color, dtype=np.float64)
    if color.ndim != 3 or color.shape[2] != 3:
        raise ValueError("view color array must be HxWx3")
    maximum_channel = color.max(axis=2)
    minimum_channel = color.min(axis=2)
    colorfulness = (maximum_channel - minimum_channel) / 255.0
    response = _convolve_same(colorfulness, _KERNEL)
    radius = int(feature_params["min_distance_px"])
    local_max = _local_max_mask(response, radius)
    threshold = float(feature_params["threshold"])
    descriptor_radius = int(feature_params["descriptor_radius"])
    height, width = response.shape
    margin = descriptor_radius + 2
    candidate_y, candidate_x = np.nonzero(
        local_max
        & (response >= threshold)
        & (np.arange(height)[:, None] >= margin)
        & (np.arange(height)[:, None] < height - margin)
        & (np.arange(width)[None, :] >= margin)
        & (np.arange(width)[None, :] < width - margin)
    )
    if len(candidate_y) == 0:
        return {
            "view_index": view.view_index,
            "detector": feature_params["detector"],
            "descriptor_dimension": 75,
            "feature_count": 0,
            "features": [],
        }

    responses = response[candidate_y, candidate_x]
    response_order = np.lexsort((candidate_x, candidate_y, -responses))
    selected: list[tuple[int, int, float]] = []
    minimum_squared = radius * radius
    for position in response_order:
        y = int(candidate_y[position])
        x = int(candidate_x[position])
        keep = True
        for selected_y, selected_x, _ in selected:
            if (y - selected_y) ** 2 + (x - selected_x) ** 2 < minimum_squared:
                keep = False
                break
        if keep:
            selected.append((y, x, float(responses[position])))
        if len(selected) >= int(feature_params["max_features_per_view"]):
            break

    selected.sort(key=lambda item: (item[0], item[1], -item[2]))
    features: list[dict[str, Any]] = []
    for ordinal, (y, x, value) in enumerate(selected, 1):
        feature_id = f"F-V{view.view_index:04d}-{ordinal:06d}"
        descriptor = _patch_descriptor(color, y, x, descriptor_radius)
        features.append(
            {
                "feature_id": feature_id,
                "view_index": view.view_index,
                "pixel": [x, y],
                "response": canonical_float(value, 10),
                "descriptor": descriptor,
            }
        )
    return {
        "view_index": view.view_index,
        "detector": feature_params["detector"],
        "descriptor_dimension": 75,
        "feature_count": len(features),
        "features": features,
    }


def payload_has_nonfinite_features(payload: dict[str, Any]) -> bool:
    for feature in payload.get("features", []):
        descriptor = np.asarray(feature.get("descriptor", []), dtype=np.float64)
        if descriptor.size == 0 or not np.isfinite(descriptor).all():
            return True
        response = feature.get("response")
        if not isinstance(response, (int, float)) or not np.isfinite(float(response)):
            return True
    return False


def inject_nonfinite_descriptor(payload: dict[str, Any]) -> int:
    features = payload.get("features", [])
    if not features:
        raise ValueError("cannot inject a non-finite descriptor into an empty feature set")
    features[0]["descriptor"][0] = float("nan")
    return int(features[0]["view_index"])
