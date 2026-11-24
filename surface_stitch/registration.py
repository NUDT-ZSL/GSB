"""FFT phase correlation and deterministic sub-pixel refinement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np

from .geometry import slice_for_shift


@dataclass(frozen=True)
class Registration:
    integer_dx: int
    integer_dy: int
    subpixel_dx: float
    subpixel_dy: float
    confidence: float
    fft_peak_norm: float
    response_peak: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "integer_offset_xy": [self.integer_dx, self.integer_dy],
            "subpixel_offset_xy": [self.subpixel_dx, self.subpixel_dy],
            "confidence": self.confidence,
            "fft_peak_norm": self.fft_peak_norm,
            "response_peak": self.response_peak,
        }


def _next_power_two(value: int) -> int:
    result = 1
    while result < value:
        result <<= 1
    return result


def _parabolic_fraction(minus: float, center: float, plus: float) -> float:
    denominator = minus - 2.0 * center + plus
    if not np.isfinite(denominator) or abs(denominator) < 1e-15:
        return 0.0
    fraction = 0.5 * (minus - plus) / denominator
    if not np.isfinite(fraction):
        return 0.0
    return float(np.clip(fraction, -0.5, 0.5))


def normalized_correlation_at_shift(
    reference: np.ndarray, moving: np.ndarray, dy: int, dx: int
) -> float:
    ref_y, ref_x, mov_y, mov_x = slice_for_shift(
        reference.shape[0], reference.shape[1], dy, dx
    )
    a = np.asarray(reference[ref_y, ref_x], dtype=np.float64).ravel()
    b = np.asarray(moving[mov_y, mov_x], dtype=np.float64).ravel()
    if a.size < 16 or np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return 0.0
    a0 = a - a.mean()
    b0 = b - b.mean()
    denominator = float(np.sqrt(np.sum(a0 * a0) * np.sum(b0 * b0)))
    if denominator <= 1e-15:
        return 0.0
    return float(np.clip(np.sum(a0 * b0) / denominator, -1.0, 1.0))


def phase_correlate(
    reference: np.ndarray,
    moving: np.ndarray,
    max_shift: int = 32,
) -> Registration:
    """Estimate offset where ``moving[k] ~= reference[k + offset]``.

    The returned ``(dx, dy)`` is therefore the offset used by
    :func:`surface_stitch.geometry.bilinear_shift_image`: destination grid
    ``k`` samples moving image at ``k - offset`` to reconstruct the reference
    nominal grid.
    """

    ref = np.asarray(reference, dtype=np.float64)
    mov = np.asarray(moving, dtype=np.float64)
    if ref.shape != mov.shape:
        raise ValueError("registration patches must have identical shapes")

    height, width = ref.shape
    ref_centered = ref - float(ref.mean())
    mov_centered = mov - float(mov.mean())

    fft_h = _next_power_two(2 * height - 1)
    fft_w = _next_power_two(2 * width - 1)
    fft_ref = np.fft.fft2(ref_centered, (fft_h, fft_w))
    fft_mov = np.fft.fft2(mov_centered, (fft_h, fft_w))
    response = np.fft.ifft2(fft_ref * np.conj(fft_mov)).real

    limit_y = min(int(max_shift), height - 2)
    limit_x = min(int(max_shift), width - 2)
    best_value = -np.inf
    best_dy = best_dx = 0
    best_index = (0, 0)
    for dy in range(-limit_y, limit_y + 1):
        iy = dy if dy >= 0 else fft_h + dy
        for dx in range(-limit_x, limit_x + 1):
            ix = dx if dx >= 0 else fft_w + dx
            value = float(response[iy, ix])
            if value > best_value:
                best_value = value
                best_dy, best_dx = dy, dx
                best_index = (iy, ix)

    peak_y, peak_x = best_index

    def response_at(dy_delta: int, dx_delta: int) -> float:
        signed_y = best_dy + dy_delta
        signed_x = best_dx + dx_delta
        iy = signed_y if signed_y >= 0 else fft_h + signed_y
        ix = signed_x if signed_x >= 0 else fft_w + signed_x
        return float(response[iy % fft_h, ix % fft_w])

    frac_y = _parabolic_fraction(
        response_at(-1, 0), best_value, response_at(1, 0)
    )
    frac_x = _parabolic_fraction(
        response_at(0, -1), best_value, response_at(0, 1)
    )

    energy_norm = float(
        best_value
        / max(
            np.sqrt(float(np.sum(ref_centered * ref_centered))
                    * float(np.sum(mov_centered * mov_centered))),
            1e-15,
        )
    )
    confidence = normalized_correlation_at_shift(
        ref, mov, best_dy, best_dx
    )
    if confidence < 0.0:
        confidence = 0.0

    return Registration(
        integer_dx=int(best_dx),
        integer_dy=int(best_dy),
        subpixel_dx=float(best_dx + frac_x),
        subpixel_dy=float(best_dy + frac_y),
        confidence=float(confidence),
        fft_peak_norm=energy_norm,
        response_peak=float(best_value),
    )
