"""Directional-light relief grayscale and global normalization."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import ReliefConfig
from .noise import synthesize_height, synthesize_height_window


@dataclass(frozen=True)
class NormalizationStats:
    raw_min: float
    raw_max: float

    def to_dict(self) -> dict[str, float]:
        return {"raw_min": self.raw_min, "raw_max": self.raw_max}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "NormalizationStats":
        return cls(raw_min=float(data["raw_min"]), raw_max=float(data["raw_max"]))


def _raw_from_gradients(
    gx: np.ndarray, gy: np.ndarray, config: ReliefConfig
) -> np.ndarray:
    nx = -config.emboss_strength * gx
    ny = -config.emboss_strength * gy
    nz = np.ones_like(nx)
    inverse_length = 1.0 / np.sqrt(nx * nx + ny * ny + 1.0)
    lx, ly, lz = config.light_direction
    return (nx * lx + ny * ly + nz * lz) * inverse_length


def raw_from_full_height(height: np.ndarray, config: ReliefConfig) -> np.ndarray:
    if height.shape != (config.height, config.width):
        raise ValueError("height array shape does not match the configured canvas")

    gx = np.empty_like(height)
    gy = np.empty_like(height)
    if config.width > 1:
        gx[:, 1:-1] = (height[:, 2:] - height[:, :-2]) * 0.5
        gx[:, 0] = height[:, 1] - height[:, 0]
        gx[:, -1] = height[:, -1] - height[:, -2]
    else:
        gx[:, 0] = 0.0
    if config.height > 1:
        gy[1:-1, :] = (height[2:, :] - height[:-2, :]) * 0.5
        gy[0, :] = height[1, :] - height[0, :]
        gy[-1, :] = height[-1, :] - height[-2, :]
    else:
        gy[0, :] = 0.0
    return _raw_from_gradients(gx, gy, config)


def raw_from_block(
    config: ReliefConfig, row: int, col: int
) -> tuple[np.ndarray, np.ndarray]:
    """Return the core raw shading and its exact global-coordinate height window."""

    x0, y0, block_width, block_height = config.block_bounds(row, col)
    x_start = max(x0 - 1, 0)
    x_end = min(x0 + block_width, config.width - 1)
    y_start = max(y0 - 1, 0)
    y_end = min(y0 + block_height, config.height - 1)
    window = synthesize_height_window(
        config,
        x_start,
        y_start,
        x_end - x_start + 1,
        y_end - y_start + 1,
    )
    height = window.values

    core_x = np.arange(x0, x0 + block_width, dtype=np.int64)
    core_y = np.arange(y0, y0 + block_height, dtype=np.int64)
    left_x = np.maximum(core_x - 1, 0)
    right_x = np.minimum(core_x + 1, config.width - 1)
    left_index = left_x - x_start
    right_index = right_x - x_start
    top_y = np.maximum(core_y - 1, 0)
    bottom_y = np.minimum(core_y + 1, config.height - 1)
    top_index = top_y - y_start
    bottom_index = bottom_y - y_start
    core_row_index = core_y - y_start
    core_col_index = core_x - x_start

    denominator_x = (right_x - left_x)[None, :].astype(np.float64)
    denominator_y = (bottom_y - top_y)[:, None].astype(np.float64)
    if config.width == 1:
        denominator_x[...] = 1.0
    if config.height == 1:
        denominator_y[...] = 1.0
    gx = (
        height[np.ix_(core_row_index, right_index)]
        - height[np.ix_(core_row_index, left_index)]
    ) / denominator_x
    gy = (
        height[np.ix_(bottom_index, core_col_index)]
        - height[np.ix_(top_index, core_col_index)]
    ) / denominator_y
    if config.width == 1:
        gx[...] = 0.0
    if config.height == 1:
        gy[...] = 0.0
    return _raw_from_gradients(gx, gy, config), window.values


def quantize_raw(
    raw: np.ndarray,
    stats: NormalizationStats,
    config: ReliefConfig,
) -> np.ndarray:
    span = stats.raw_max - stats.raw_min
    if span <= 0.0:
        normalized = np.zeros_like(raw)
    else:
        normalized = np.clip((raw - stats.raw_min) / span, 0.0, 1.0)
    level_index = np.floor(
        normalized * float(config.quantization_levels - 1) + 0.5
    )
    level_index = np.clip(
        level_index, 0.0, float(config.quantization_levels - 1)
    )
    gray = (
        config.normalization_min
        + np.floor(
            level_index
            * float(config.normalization_span)
            / float(config.quantization_levels - 1)
            + 0.5
        )
    )
    return gray.astype(np.uint16)


def compute_normalization_stats(config: ReliefConfig) -> NormalizationStats:
    """Compute exact full-canvas extrema using the same per-pixel arithmetic.

    The block memory budget controls in-flight tile buffers. This one-time
    normalization scan is not a tile buffer; its result is persisted so a cached
    preview does not repeat it.
    """

    height = synthesize_height(config)
    raw = raw_from_full_height(height, config)
    return NormalizationStats(
        raw_min=float(np.min(raw)),
        raw_max=float(np.max(raw)),
    )


def render_block_raw(
    config: ReliefConfig, row: int, col: int
) -> tuple[np.ndarray, np.ndarray]:
    return raw_from_block(config, row, col)


def render_block_gray(
    config: ReliefConfig,
    row: int,
    col: int,
    stats: NormalizationStats,
) -> tuple[np.ndarray, np.ndarray]:
    raw, height_window = raw_from_block(config, row, col)
    return quantize_raw(raw, stats, config), height_window


def generate_full_gray(config: ReliefConfig) -> np.ndarray:
    height = synthesize_height(config)
    raw = raw_from_full_height(height, config)
    stats = NormalizationStats(
        raw_min=float(np.min(raw)),
        raw_max=float(np.max(raw)),
    )
    return quantize_raw(raw, stats, config)


def generate_full_raw(config: ReliefConfig) -> np.ndarray:
    return raw_from_full_height(synthesize_height(config), config)
