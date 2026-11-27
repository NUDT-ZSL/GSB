"""Coordinate-derived deterministic dithering."""

from __future__ import annotations

import numpy as np


_MASK64 = np.uint64(0xFFFFFFFFFFFFFFFF)
_C1 = np.uint64(0x9E3779B185EBCA87)
_C2 = np.uint64(0xC2B2AE3D27D4EB4F)
_C4 = np.uint64(0xBF58476D1CE4E5B9)
_C5 = np.uint64(0x94D049BB133111EB)


def _splitmix64(value: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        z = (value + _C4) & _MASK64
        z = ((z ^ (z >> np.uint64(30))) * _C5) & _MASK64
        z = ((z ^ (z >> np.uint64(27))) * _C4) & _MASK64
        z = z ^ (z >> np.uint64(31))
    return z


def deterministic_uniform(
    width: int,
    height: int,
    seed: int,
    *,
    channel: int = 0,
) -> np.ndarray:
    """Return a float64 [0, 1) field reproducible on a little-endian host."""
    x = np.arange(width, dtype=np.uint64)[None, :]
    y = np.arange(height, dtype=np.uint64)[:, None]
    with np.errstate(over="ignore"):
        key = (
            (x * _C1)
            ^ (y * _C2)
            ^ np.uint64(seed & 0xFFFFFFFFFFFFFFFF)
            ^ np.uint64(channel & 0xFFFFFFFFFFFFFFFF)
        )
        key = key + (y << np.uint64(17)) + (x << np.uint64(31))
    bits = _splitmix64(key)
    return (bits >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def build_dither_offsets(
    mask: np.ndarray,
    *,
    seed: int,
    texture_amplitudes: dict[str, float],
    texture_by_label: dict[int, str],
    background_label: int = 0,
) -> tuple[np.ndarray, dict[int, float]]:
    """Build per-pixel threshold offsets whose amplitude follows part texture."""
    amplitude_by_label: dict[int, float] = {
        int(label): float(texture_amplitudes.get(texture, texture_amplitudes["flat"]))
        for label, texture in texture_by_label.items()
    }
    amplitude_by_label[int(background_label)] = float(texture_amplitudes["flat"])
    uniform = deterministic_uniform(mask.shape[1], mask.shape[0], seed, channel=0xA17E)
    amplitude_map = np.full(mask.shape, amplitude_by_label[int(background_label)], dtype=np.float64)
    for label, amplitude in amplitude_by_label.items():
        amplitude_map[mask == label] = amplitude
    offsets = (2.0 * uniform - 1.0) * amplitude_map
    return offsets.astype(np.float64), amplitude_by_label
