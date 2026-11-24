from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .config import WorkbenchConfig

_GRADIENT_TABLE_SIZE = 256
_MASK = np.uint64(0xFFFFFFFFFFFFFFFF)


def _mix64(value: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        value = value.astype(np.uint64, copy=False)
        value ^= value >> np.uint64(30)
        value *= np.uint64(0xBF58476D1CE4E5B9)
        value ^= value >> np.uint64(27)
        value *= np.uint64(0x94D049BB133111EB)
        value ^= value >> np.uint64(31)
        return value & _MASK


def _fade(value: np.ndarray) -> np.ndarray:
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


@dataclass(frozen=True)
class GradientTable:
    vectors: np.ndarray


class PatternSynthesizer:
    """Perlin-style gradient noise evaluated only in global pixel coordinates."""

    def __init__(self, config: WorkbenchConfig) -> None:
        self.config = config
        self._frequencies = np.asarray(
            [octave.frequency for octave in config.octaves], dtype=np.float64
        )
        self._amplitudes = np.asarray(
            [octave.amplitude for octave in config.octaves], dtype=np.float64
        )
        self._amplitude_sum = float(np.sum(self._amplitudes))
        self._table = self._build_gradient_table(config.seed)

    @staticmethod
    def _build_gradient_table(seed: int) -> np.ndarray:
        rng = np.random.Generator(np.random.PCG64(seed))
        angles = rng.uniform(0.0, 2.0 * math.pi, size=_GRADIENT_TABLE_SIZE)
        table = np.empty((_GRADIENT_TABLE_SIZE, 2), dtype=np.float64)
        table[:, 0] = np.cos(angles)
        table[:, 1] = np.sin(angles)
        table.setflags(write=False)
        return table

    def _gradient_indices(
        self, ix: np.ndarray, iy: np.ndarray, octave_index: int
    ) -> np.ndarray:
        seed = np.uint64(self.config.seed & 0xFFFFFFFFFFFFFFFF)
        octave = np.uint64((octave_index + 1) * 0x9E3779B97F4A7C15 & 0xFFFFFFFFFFFFFFFF)
        mixed = (
            _mix64(ix.astype(np.uint64, copy=False) * np.uint64(0xD6E8FEB86659FD93))
            ^ _mix64(iy.astype(np.uint64, copy=False) * np.uint64(0xA24BAED4963EE407))
            ^ _mix64(seed ^ octave)
        )
        return (mixed & np.uint64(_GRADIENT_TABLE_SIZE - 1)).astype(np.intp)

    def height(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """Evaluate a rectangular global-coordinate height patch.

        ``xs`` and ``ys`` are one-dimensional absolute canvas coordinates.
        The returned float64 array is indexed as ``[y, x]``.
        """

        x_values = np.asarray(xs, dtype=np.float64)
        y_values = np.asarray(ys, dtype=np.float64)
        height = np.zeros((y_values.size, x_values.size), dtype=np.float64)

        for octave_index, (frequency, amplitude) in enumerate(
            zip(self._frequencies, self._amplitudes, strict=True)
        ):
            gx = x_values[None, :] * frequency
            gy = y_values[:, None] * frequency
            x0 = np.floor(gx)
            y0 = np.floor(gy)
            xf = gx - x0
            yf = gy - y0
            ix0 = x0.astype(np.int64)
            iy0 = y0.astype(np.int64)
            ix1 = ix0 + 1
            iy1 = iy0 + 1

            idx00 = self._gradient_indices(ix0, iy0, octave_index)
            idx10 = self._gradient_indices(ix1, iy0, octave_index)
            idx01 = self._gradient_indices(ix0, iy1, octave_index)
            idx11 = self._gradient_indices(ix1, iy1, octave_index)
            table = self._table

            xf_b = np.broadcast_to(xf, idx00.shape)
            yf_b = np.broadcast_to(yf, idx00.shape)
            n00 = table[idx00, 0] * xf_b + table[idx00, 1] * yf_b
            n10 = table[idx10, 0] * (xf_b - 1.0) + table[idx10, 1] * yf_b
            n01 = table[idx01, 0] * xf_b + table[idx01, 1] * (yf_b - 1.0)
            n11 = (
                table[idx11, 0] * (xf_b - 1.0)
                + table[idx11, 1] * (yf_b - 1.0)
            )
            u = _fade(xf_b)
            v = _fade(yf_b)
            nx0 = n00 + u * (n10 - n00)
            nx1 = n01 + u * (n11 - n01)
            height += amplitude * (nx0 + v * (nx1 - nx0))

        height /= self._amplitude_sum
        return height

    def height_patch(
        self, x0: int, x1: int, y0: int, y1: int
    ) -> np.ndarray:
        return self.height(
            np.arange(x0, x1, dtype=np.float64),
            np.arange(y0, y1, dtype=np.float64),
        )
