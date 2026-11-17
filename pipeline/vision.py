"""Deterministic visual feature extraction and quantization."""

from __future__ import annotations

import numpy as np

from .config import FusionConfig

#: Raw statistics produced per region: mean, gradient strength, four
#: orientation histogram bins. The configured feature dimension must be a
#: multiple of this count so vectors are formed by deterministic tiling.
BASE_FEATURE_COUNT = 6


def _region_bounds(config: FusionConfig) -> list[tuple[int, int, int, int]]:
    cell_width = config.image_width // config.grid_cols
    cell_height = config.image_height // config.grid_rows
    bounds = []
    for row in range(config.grid_rows):
        for col in range(config.grid_cols):
            y0 = row * cell_height
            y1 = config.image_height if row == config.grid_rows - 1 \
                else (row + 1) * cell_height
            x0 = col * cell_width
            x1 = config.image_width if col == config.grid_cols - 1 \
                else (col + 1) * cell_width
            bounds.append((y0, y1, x0, x1))
    return bounds


def _region_statistics(block: np.ndarray) -> np.ndarray:
    gray = (0.299 * block[:, :, 0]
            + 0.587 * block[:, :, 1]
            + 0.114 * block[:, :, 2]).astype(np.float64)
    mean_value = float(gray.mean()) / 255.0

    gy = np.zeros_like(gray)
    gx = np.zeros_like(gray)
    gy[1:, :] = gray[1:, :] - gray[:-1, :]
    gx[:, 1:] = gray[:, 1:] - gray[:, :-1]
    magnitude = np.hypot(gx, gy)
    gradient_strength = float(magnitude.mean()) / 255.0

    # Four-bin orientation histogram (0-45, 45-90, 90-135, 135-180 degrees),
    # weighted by gradient magnitude. Bins are fixed, no RNG involved.
    histogram = np.zeros(4, dtype=np.float64)
    nonzero = magnitude > 1e-12
    if np.any(nonzero):
        angle = (np.degrees(np.arctan2(gy[nonzero], gx[nonzero])) + 180.0)
        bin_index = np.minimum((angle / 45.0).astype(np.int64), 3)
        np.add.at(histogram, bin_index, magnitude[nonzero])
        total = histogram.sum()
        if total > 0:
            histogram /= total
    return np.concatenate(([mean_value, gradient_strength], histogram))


def _l2_normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        return np.zeros_like(vector)
    return vector / norm


def extract_region_features(image: np.ndarray,
                            config: FusionConfig) -> np.ndarray:
    """Extract one normalized feature vector per candidate region.

    Returns an array of shape ``(region_count, feature_dim)``. Statistics are
    computed on deterministic integer-sliced blocks and tiled without any
    randomness, so identical inputs always produce element-identical output.
    """
    if image.dtype != np.uint8 or image.ndim != 3 \
            or image.shape != (config.image_height, config.image_width, 3):
        raise ValueError("image must be a uint8 HxWx3 array matching config")
    if config.feature_dim % BASE_FEATURE_COUNT != 0:
        raise ValueError("feature_dim must be a multiple of "
                         f"{BASE_FEATURE_COUNT}")

    rows = []
    for y0, y1, x0, x1 in _region_bounds(config):
        base = _region_statistics(image[y0:y1, x0:x1, :])
        repeats = config.feature_dim // BASE_FEATURE_COUNT
        vector = np.tile(base, repeats)
        rows.append(_l2_normalize(vector))
    return np.vstack(rows).astype(np.float64)


def quantize_features(features: np.ndarray,
                      levels: int) -> np.ndarray:
    """Deterministically discretize each feature into ``levels`` bins.

    Min-max normalization maps each vector to [0, 1]; values are then rounded
    onto a uniform grid with ``levels`` steps and rescaled back to [0, 1].
    Changing ``levels`` changes the rounding grid, hence the discrete output;
    identical inputs always return element-identical arrays.
    """
    if levels < 2:
        raise ValueError("quantization levels must be >= 2")
    quantized = np.zeros_like(features, dtype=np.float64)
    for index, vector in enumerate(features):
        minimum = float(vector.min())
        maximum = float(vector.max())
        if maximum - minimum <= 1e-12:
            quantized[index, :] = 0.0
            continue
        scaled = (vector - minimum) / (maximum - minimum)
        bins = np.round(scaled * (levels - 1)) / (levels - 1)
        quantized[index, :] = bins
    return quantized


def visual_pipeline(image: np.ndarray,
                    config: FusionConfig) -> np.ndarray:
    """Extract then quantize region features in one deterministic call."""
    raw = extract_region_features(image, config)
    if not np.all(np.isfinite(raw)):
        raise ValueError("non-finite value produced during visual extraction")
    return quantize_features(raw, config.quantization_levels)


def quantization_diagnostics(config: FusionConfig) -> dict:
    """Static diagnostic metadata echoed into the report."""
    return {
        "feature_dim": config.feature_dim,
        "quantization_levels": config.quantization_levels,
        "base_feature_count": BASE_FEATURE_COUNT,
        "region_count": config.region_count,
        "grid": [config.grid_rows, config.grid_cols],
    }
