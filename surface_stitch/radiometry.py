"""Linear brightness/contrast estimation and correction."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import slice_for_shift


@dataclass(frozen=True)
class Radiometry:
    scale: float
    offset: float

    def apply(self, image: np.ndarray) -> np.ndarray:
        return np.asarray(image, dtype=np.float64) * self.scale + self.offset

    def to_dict(self) -> dict:
        return {"scale": self.scale, "offset": self.offset}


def estimate_linear_correction(
    reference: np.ndarray,
    moving: np.ndarray,
    shift_dy: int,
    shift_dx: int,
) -> Radiometry:
    """Fit ``reference ~= scale * moving + offset`` on valid shifted samples."""

    ref_y, ref_x, mov_y, mov_x = slice_for_shift(
        reference.shape[0], reference.shape[1], shift_dy, shift_dx
    )
    ref_blurred = box_blur(reference, 2)
    mov_blurred = box_blur(moving, 2)
    ref_values = np.asarray(ref_blurred[ref_y, ref_x], dtype=np.float64).ravel()
    mov_values = np.asarray(mov_blurred[mov_y, mov_x], dtype=np.float64).ravel()
    if ref_values.size < 64:
        return Radiometry(1.0, 0.0)

    mov_mean = float(mov_values.mean())
    ref_mean = float(ref_values.mean())
    centered_mov = mov_values - mov_mean
    centered_ref = ref_values - ref_mean
    variance = float(np.sum(centered_mov * centered_mov))
    if variance < 1e-8:
        return Radiometry(1.0, ref_mean - mov_mean)

    scale = float(np.sum(centered_mov * centered_ref) / variance)
    if not np.isfinite(scale) or scale <= 0.0:
        scale = 1.0
    scale = float(np.clip(scale, 0.5, 2.0))
    offset = ref_mean - scale * mov_mean
    if not np.isfinite(offset):
        offset = 0.0
    return Radiometry(scale=scale, offset=float(offset))


def mean_absolute_difference(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0:
        return 0.0
    return float(np.mean(np.abs(a.astype(np.float64) - b.astype(np.float64))))


def box_blur(image: np.ndarray, radius: int = 2) -> np.ndarray:
    """Deterministic edge-padded box blur used only for coefficient fitting."""

    if radius <= 0:
        return np.asarray(image, dtype=np.float64)
    kernel_size = 2 * int(radius) + 1
    padded = np.pad(np.asarray(image, dtype=np.float64), radius, mode="edge")
    height, width = image.shape
    result = np.zeros((height, width), dtype=np.float64)
    for dy in range(kernel_size):
        for dx in range(kernel_size):
            result += padded[dy:dy + height, dx:dx + width]
    return result / float(kernel_size * kernel_size)
