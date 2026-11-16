from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np

from .config import CLASS_IDS, SegmentationConfig
from .model import TileData, TileResult


@dataclass(frozen=True)
class WindowSpec:
    y0: int
    y1: int
    x0: int
    x1: int
    context: int


def iter_windows(height: int, width: int, config: SegmentationConfig) -> Iterator[WindowSpec]:
    context = config.context_overlap
    for y0 in range(0, height, config.stride_height):
        y1 = min(height, y0 + config.window_height)
        for x0 in range(0, width, config.stride_width):
            x1 = min(width, x0 + config.window_width)
            yield WindowSpec(y0, y1, x0, x1, context)


def _with_context(
    bands: np.ndarray,
    window: WindowSpec,
) -> tuple[np.ndarray, int, int]:
    height, width = bands.shape[:2]
    context = window.context
    cy0 = max(0, window.y0 - context)
    cy1 = min(height, window.y1 + context)
    cx0 = max(0, window.x0 - context)
    cx1 = min(width, window.x1 + context)
    crop = bands[cy0:cy1, cx0:cx1, :]
    pad_top = window.y0 - cy0
    pad_bottom = cy1 - window.y1
    pad_left = window.x0 - cx0
    pad_right = cx1 - window.x1
    if pad_top or pad_bottom or pad_left or pad_right:
        mode = "reflect" if crop.shape[0] > 1 and crop.shape[1] > 1 else "edge"
        crop = np.pad(
            crop,
            ((pad_top, pad_bottom), (pad_left, pad_right), (0, 0)),
            mode=mode,
        )
    return crop, window.y0 - cy0, window.x0 - cx0


def _central_gradient(values: np.ndarray, axis: int) -> np.ndarray:
    if values.shape[axis] < 2:
        return np.zeros_like(values)
    return np.gradient(values, axis=axis, edge_order=1)


def _box3_mean(mask: np.ndarray) -> np.ndarray:
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return mask.astype(np.float64)
    padded = np.pad(mask.astype(np.float64), 1, mode="edge")
    total = np.zeros_like(mask, dtype=np.float64)
    for dy in range(3):
        for dx in range(3):
            total += padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
    return total / 9.0


def _window_feature_maps(crop: np.ndarray, config: SegmentationConfig) -> dict[str, np.ndarray]:
    red = crop[:, :, 0]
    green = crop[:, :, 1]
    nir = crop[:, :, 2]
    elevation = crop[:, :, 3]
    eps = config.epsilon
    ndvi = np.clip((nir - red) / (nir + red + eps), -1.0, 1.0)
    ndwi = np.clip((green - nir) / (green + nir + eps), -1.0, 1.0)
    gradient_y = _central_gradient(nir, axis=0)
    gradient_x = _central_gradient(nir, axis=1)
    edge_magnitude = np.sqrt(gradient_x * gradient_x + gradient_y * gradient_y)
    edge_mask = edge_magnitude >= config.feature_thresholds["edge_density"]
    edge_density = np.clip(_box3_mean(edge_mask) * config.edge_scale, 0.0, 1.0)
    slope_y = _central_gradient(elevation, axis=0)
    slope_x = _central_gradient(elevation, axis=1)
    slope = np.clip(
        np.sqrt(slope_x * slope_x + slope_y * slope_y) * config.slope_scale,
        0.0,
        1.0,
    )
    brightness = np.clip((red + green + nir) / 3.0, 0.0, 1.0)
    return {
        "ndvi": ndvi,
        "ndwi": ndwi,
        "edge_density": edge_density,
        "slope": slope,
        "brightness": brightness,
        "elevation": elevation,
    }


def _score_features(features: dict[str, np.ndarray], config: SegmentationConfig) -> np.ndarray:
    shape = next(iter(features.values())).shape
    scores = np.empty((len(CLASS_IDS), shape[0], shape[1]), dtype=np.float64)
    for index, class_id in enumerate(CLASS_IDS):
        weights = config.class_weights[class_id]
        score = np.full(shape, float(weights.get("bias", 0.0)), dtype=np.float64)
        for name, values in features.items():
            weight = float(weights.get(name, 0.0))
            if weight != 0.0:
                score += weight * (values - config.feature_thresholds[name])
        scores[index] = score
    # Explicit rounding prevents platform FMA differences from leaking into ties.
    return np.round(scores, decimals=10)


def extract_and_classify(tile: TileData, config: SegmentationConfig) -> TileResult:
    height, width = tile.meta.expected_shape
    classes = np.zeros((height, width), dtype=np.uint8)
    scores = np.full((len(CLASS_IDS), height, width), np.nan, dtype=np.float64)
    window_count = 0
    for window in iter_windows(height, width, config):
        window_count += 1
        crop, offset_y, offset_x = _with_context(tile.bands, window)
        feature_maps = _window_feature_maps(crop, config)
        window_scores = _score_features(feature_maps, config)
        out_h = window.y1 - window.y0
        out_w = window.x1 - window.x0
        local = window_scores[:, offset_y : offset_y + out_h, offset_x : offset_x + out_w]
        winner = np.argmax(local, axis=0).astype(np.uint8) + 1
        scores[:, window.y0 : window.y1, window.x0 : window.x1] = local
        classes[window.y0 : window.y1, window.x0 : window.x1] = winner
    if np.isnan(scores).any():
        raise RuntimeError("sliding windows did not cover every tile pixel")
    return TileResult(
        row=tile.meta.row,
        col=tile.meta.col,
        cache_key="",
        classes=classes,
        scores=np.ascontiguousarray(scores),
        band_digest=tile.array_digest,
        window_count=window_count,
    )
