from __future__ import annotations

import numpy as np

from surface_stitch.registration import phase_correlate


def test_phase_correlation_detects_known_integer_shift_direction() -> None:
    rng = np.random.default_rng(7)
    base = rng.normal(128, 18, (96, 96))
    base += 20 * np.sin(np.mgrid[0:96, 0:96][1] / 7.0)
    moving = np.zeros_like(base)
    # moving[k] = base[k + (dy=3, dx=-5)]
    moving[:-3, 5:] = base[3:, :-5]
    result = phase_correlate(base, moving, max_shift=16)
    assert (result.integer_dx, result.integer_dy) == (-5, 3)
    assert result.confidence > 0.9


def test_parabolic_subpixel_estimate_is_reasonable() -> None:
    rng = np.random.default_rng(11)
    texture = rng.normal(0, 20, (160, 160))
    texture = (texture + np.roll(texture, 1, 0) + np.roll(texture, -1, 0)) / 3
    dx, dy = 2.25, -1.75
    yy, xx = np.mgrid[0:96, 0:96]
    sx = 32 + xx + dx
    sy = 16 + yy + dy
    x0 = np.floor(sx).astype(int)
    y0 = np.floor(sy).astype(int)
    fx = sx - x0
    fy = sy - y0
    moving = (
        texture[y0, x0] * (1 - fx) * (1 - fy)
        + texture[y0, x0 + 1] * fx * (1 - fy)
        + texture[y0 + 1, x0] * (1 - fx) * fy
        + texture[y0 + 1, x0 + 1] * fx * fy
    )
    reference = texture[16:112, 32:128]
    result = phase_correlate(reference, moving, max_shift=12)
    assert result.integer_dx == 2
    assert result.integer_dy == -2
    assert abs(result.subpixel_dx - dx) < 0.35
    assert abs(result.subpixel_dy - dy) < 0.35
