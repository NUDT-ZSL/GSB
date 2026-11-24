"""Global-coordinate deterministic gradient noise."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .config import ReliefConfig


_MASK64 = (1 << 64) - 1
_SQRT_HALF = 0.7071067811865476
_COS_PI_8 = 0.9238795325112867
_SIN_PI_8 = 0.3826834323650898
_BASE_GRADIENTS = np.asarray(
    (
        (1.0, 0.0),
        (_COS_PI_8, _SIN_PI_8),
        (_SQRT_HALF, _SQRT_HALF),
        (_SIN_PI_8, _COS_PI_8),
        (0.0, 1.0),
        (-_SIN_PI_8, _COS_PI_8),
        (-_SQRT_HALF, _SQRT_HALF),
        (-_COS_PI_8, _SIN_PI_8),
        (-1.0, 0.0),
        (-_COS_PI_8, -_SIN_PI_8),
        (-_SQRT_HALF, -_SQRT_HALF),
        (-_SIN_PI_8, -_COS_PI_8),
        (0.0, -1.0),
        (_SIN_PI_8, -_COS_PI_8),
        (_SQRT_HALF, -_SQRT_HALF),
        (_COS_PI_8, -_SIN_PI_8),
    ),
    dtype=np.float64,
)
_BASE_GRADIENTS.setflags(write=False)


def _splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & _MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return (value ^ (value >> 31)) & _MASK64


@lru_cache(maxsize=16)
def gradient_table(seed: int, table_size: int) -> np.ndarray:
    """Build an immutable unit-gradient table from the seed only."""

    order = list(range(_BASE_GRADIENTS.shape[0]))
    state = seed & _MASK64
    for index in range(len(order) - 1, 0, -1):
        state = _splitmix64(state)
        swap_index = state % (index + 1)
        order[index], order[swap_index] = order[swap_index], order[index]
    gradients = np.empty((table_size, 2), dtype=np.float64)
    for index in range(table_size):
        gradients[index] = _BASE_GRADIENTS[order[index % len(order)]]
    gradients.setflags(write=False)
    return gradients


def _hash_cells(
    ix: np.ndarray,
    iy: np.ndarray,
    seed: int,
    octave_index: int,
    table_size: int,
) -> np.ndarray:
    ux = ix.astype(np.uint64, copy=False)
    uy = iy.astype(np.uint64, copy=False)
    with np.errstate(over="ignore"):
        hashed = ux * np.uint64(0x9E3779B97F4A7C15)
        hashed ^= uy * np.uint64(0xC2B2AE3D27D4EB4F)
        hashed ^= np.uint64(seed & _MASK64)
        hashed ^= np.uint64(((octave_index + 1) * 0xD6E8FEB86659FD93) & _MASK64)
        hashed ^= hashed >> np.uint64(30)
        hashed *= np.uint64(0xBF58476D1CE4E5B9)
        hashed ^= hashed >> np.uint64(27)
        hashed *= np.uint64(0x94D049BB133111EB)
        hashed ^= hashed >> np.uint64(31)
    return (hashed & np.uint64(table_size - 1)).astype(np.intp)


def _fade(value: np.ndarray) -> np.ndarray:
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


def _lerp(left: np.ndarray, right: np.ndarray, weight: np.ndarray) -> np.ndarray:
    return left + weight * (right - left)


def _octave_height(
    x: np.ndarray,
    y: np.ndarray,
    config: ReliefConfig,
    octave_index: int,
    frequency: float,
) -> np.ndarray:
    """Perlin-style noise at integer global coordinates."""

    sx = x.astype(np.float64, copy=False) * frequency / float(config.width)
    sy = y.astype(np.float64, copy=False) * frequency / float(config.height)
    ix = np.floor(sx).astype(np.int64)
    iy = np.floor(sy).astype(np.int64)
    fx = sx - ix
    fy = sy - iy

    table = gradient_table(config.seed, config.gradient_table_size)
    hashes = (
        _hash_cells(ix, iy, config.seed, octave_index, config.gradient_table_size),
        _hash_cells(
            ix + 1, iy, config.seed, octave_index, config.gradient_table_size
        ),
        _hash_cells(
            ix, iy + 1, config.seed, octave_index, config.gradient_table_size
        ),
        _hash_cells(
            ix + 1,
            iy + 1,
            config.seed,
            octave_index,
            config.gradient_table_size,
        ),
    )

    dot00 = table[hashes[0], 0] * fx + table[hashes[0], 1] * fy
    dot10 = table[hashes[1], 0] * (fx - 1.0) + table[hashes[1], 1] * fy
    dot01 = table[hashes[2], 0] * fx + table[hashes[2], 1] * (fy - 1.0)
    dot11 = (
        table[hashes[3], 0] * (fx - 1.0)
        + table[hashes[3], 1] * (fy - 1.0)
    )

    u = _fade(fx)
    v = _fade(fy)
    return _lerp(_lerp(dot00, dot10, u), _lerp(dot01, dot11, u), v)


@dataclass(frozen=True)
class HeightWindow:
    """Height samples on a rectangle in global integer coordinates."""

    x0: int
    y0: int
    values: np.ndarray

    @property
    def x1(self) -> int:
        return self.x0 + self.values.shape[1]

    @property
    def y1(self) -> int:
        return self.y0 + self.values.shape[0]


def synthesize_height_window(
    config: ReliefConfig, x0: int, y0: int, width: int, height: int
) -> HeightWindow:
    if width <= 0 or height <= 0:
        raise ValueError("height window dimensions must be positive")
    x_values = np.arange(x0, x0 + width, dtype=np.int64)
    y_values = np.arange(y0, y0 + height, dtype=np.int64)
    x_grid, y_grid = np.meshgrid(x_values, y_values, indexing="xy")
    result = np.zeros((height, width), dtype=np.float64)
    for octave_index, octave in enumerate(config.octaves):
        result += octave.amplitude * _octave_height(
            x_grid, y_grid, config, octave_index, octave.frequency
        )
    return HeightWindow(x0=x0, y0=y0, values=result)


def synthesize_height(config: ReliefConfig) -> np.ndarray:
    return synthesize_height_window(
        config, 0, 0, config.width, config.height
    ).values


def synthesize_block_height(
    config: ReliefConfig, row: int, col: int
) -> np.ndarray:
    x0, y0, width, height = config.block_bounds(row, col)
    return synthesize_height_window(config, x0, y0, width, height).values
