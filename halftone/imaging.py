from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .config import ValidatedJob
from .errors import BusinessError


def rational_scale(source_size: int, output_size: int) -> Fraction:
    return Fraction(output_size, source_size)


def rational_resample(gray: np.ndarray, output_width: int, output_height: int) -> np.ndarray:
    """Deterministic bilinear resampling using exact rational center mapping."""

    source = np.asarray(gray, dtype=np.uint8)
    if source.ndim != 2:
        raise BusinessError("E_SOURCE", "source image must be grayscale")
    source_h, source_w = source.shape
    if (source_w, source_h) == (output_width, output_height):
        return np.ascontiguousarray(source)

    source_f = source.astype(np.float64)
    x_index = np.arange(output_width, dtype=np.int64)
    y_index = np.arange(output_height, dtype=np.int64)

    num_x = (2 * x_index + 1) * source_w - output_width
    den_x = 2 * output_width
    x0 = num_x // den_x
    wx = (num_x - x0 * den_x).astype(np.float64) / float(den_x)
    x0c = np.clip(x0, 0, source_w - 1)
    x1c = np.clip(x0 + 1, 0, source_w - 1)

    num_y = (2 * y_index + 1) * source_h - output_height
    den_y = 2 * output_height
    y0 = num_y // den_y
    wy = (num_y - y0 * den_y).astype(np.float64) / float(den_y)
    y0c = np.clip(y0, 0, source_h - 1)
    y1c = np.clip(y0 + 1, 0, source_h - 1)

    row_top_0 = source_f[y0c[:, None], x0c[None, :]]
    row_top_1 = source_f[y0c[:, None], x1c[None, :]]
    top = row_top_0 * (1.0 - wx[None, :]) + row_top_1 * wx[None, :]

    row_bottom_0 = source_f[y1c[:, None], x0c[None, :]]
    row_bottom_1 = source_f[y1c[:, None], x1c[None, :]]
    bottom = row_bottom_0 * (1.0 - wx[None, :]) + row_bottom_1 * wx[None, :]

    sampled = top * (1.0 - wy[:, None]) + bottom * wy[:, None]
    return np.clip(np.floor(sampled + 0.5), 0.0, 255.0).astype(np.uint8)


def quantize_coverage(gray: np.ndarray, job: ValidatedJob) -> Tuple[np.ndarray, np.ndarray]:
    ink_demand = 1.0 - gray.astype(np.float64) / 255.0
    capped = np.minimum(ink_demand, job.ink_coverage_max)
    level_count = job.levels - 1
    level_index = np.floor(capped * level_count + 0.5)
    level_index = np.clip(level_index, 0, level_count).astype(np.uint16)
    coverage_q = level_index.astype(np.float64) / float(level_count)
    ink_byte = np.rint(
        level_index.astype(np.float64) * 255.0 / float(level_count)
    ).astype(np.uint8)
    return level_index, coverage_q


def screen_phases(
    job: ValidatedJob,
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return centered cell phases in [-0.5, 0.5).

    Coordinates are absolute output-pixel-center coordinates.  A tile passes
    its global ``origin_x/origin_y``; therefore phase never depends on tile
    boundaries.
    """

    x_centers = np.arange(origin_x, origin_x + width, dtype=np.float64) + 0.5
    y_centers = np.arange(origin_y, origin_y + height, dtype=np.float64) + 0.5
    theta = np.deg2rad(job.angle_deg)
    cosine = np.cos(theta)
    sine = np.sin(theta)
    pitch = job.mesh_pitch_px

    global_x = x_centers[None, :]
    global_y = y_centers[:, None]
    screen_u = global_x * cosine + global_y * sine
    screen_v = -global_x * sine + global_y * cosine
    phase_u = screen_u / pitch
    phase_v = screen_v / pitch
    centered_u = phase_u - np.floor(phase_u) - 0.5
    centered_v = phase_v - np.floor(phase_v) - 0.5
    return centered_u, centered_v


def render_halftone(
    output_gray: np.ndarray,
    job: ValidatedJob,
    *,
    origin_x: int = 0,
    origin_y: int = 0,
) -> np.ndarray:
    """Render a tile when origin is its global offset, or the full image at 0,0."""

    height, width = output_gray.shape
    ink_demand = 1.0 - output_gray.astype(np.float64) / 255.0
    coverage = np.minimum(ink_demand, job.ink_coverage_max)
    phase_u, phase_v = screen_phases(job, origin_x, origin_y, width, height)

    if job.dot_shape == "square":
        shape_area = 4.0 * np.maximum(np.abs(phase_u), np.abs(phase_v)) ** 2
    elif job.dot_shape == "ellipse":
        aspect = job.ellipse_aspect
        shape_area = np.pi * (
            (phase_u / aspect) ** 2 + (phase_v * aspect) ** 2
        )
    else:
        shape_area = np.pi * (phase_u * phase_u + phase_v * phase_v)

    # A is an area-calibrated phase statistic.  The dot boundary is A ==
    # coverage and can equivalently be read as the product/ratio relation
    # A / coverage <= 1.  A narrow deterministic transition supplies the
    # finite anti-alias/ink levels without changing the screen geometry.
    edge_width = 0.18
    raw_ink = np.clip(
        0.5 + (coverage + 1e-12 - shape_area) / edge_width,
        0.0,
        1.0,
    )
    raw_ink = np.where(coverage > 0.0, raw_ink, 0.0)
    level_scale = float(job.levels - 1)
    level_index = np.floor(raw_ink * level_scale + 0.5)
    level_index = np.clip(level_index, 0, job.levels - 1).astype(np.uint16)

    device_gray = np.full((height, width), 255, dtype=np.uint8)
    ink_byte = np.rint(
        level_index.astype(np.float64) * 255.0 / float(job.levels - 1)
    ).astype(np.uint8)
    values = 255 - ink_byte.astype(np.int16)
    nonzero = level_index > 0
    device_gray[nonzero] = values[nonzero].astype(np.uint8)
    return device_gray


def ink_tone_preview(device_gray: np.ndarray, ink_rgb: Tuple[int, int, int]) -> np.ndarray:
    ink_amount = (255.0 - device_gray.astype(np.float64)) / 255.0
    ink = np.asarray(ink_rgb, dtype=np.float64)[None, None, :]
    rgb = 255.0 * (1.0 - ink_amount[:, :, None]) + ink * ink_amount[:, :, None]
    return np.rint(rgb).astype(np.uint8)


def first_consistency_mismatch(
    assembled: np.ndarray,
    reference: np.ndarray,
    job: ValidatedJob,
) -> Optional[Dict[str, Any]]:
    if assembled.shape != reference.shape:
        return {
            "shape_mismatch": True,
            "assembled_shape": list(assembled.shape),
            "reference_shape": list(reference.shape),
        }
    diff = np.asarray(assembled) != np.asarray(reference)
    if not np.any(diff):
        return None
    flat_index = int(np.argmax(diff))
    y, x = divmod(flat_index, reference.shape[1])
    tile_x = x // job.tile_width
    tile_y = y // job.tile_height
    return {
        "x": int(x),
        "y": int(y),
        "tile_x": int(tile_x),
        "tile_y": int(tile_y),
        "tile_index": int(tile_y * job.tile_cols + tile_x),
        "tile_value": _json_scalar(assembled[y, x]),
        "full_value": _json_scalar(reference[y, x]),
    }


def _json_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


def require_consistent(assembled: np.ndarray, reference: np.ndarray, job: ValidatedJob) -> None:
    mismatch = first_consistency_mismatch(assembled, reference, job)
    if mismatch is not None:
        raise BusinessError(
            "E_CONSIST",
            "assembled tiles differ from full-coordinate render",
            {"first_mismatch": mismatch},
        )


def moire_diagnostic(dot_image: np.ndarray, job: ValidatedJob) -> Dict[str, Any]:
    if job.valid_region is not None:
        rx, ry, rw, rh = job.valid_region
    else:
        rx = ry = 0
        rw, rh = job.output_width, job.output_height
    crop = dot_image[ry : ry + rh, rx : rx + rw]
    if crop.shape != (rh, rw):
        raise BusinessError("E_REGION", "valid_region cannot be read from output")

    ink = (255.0 - crop.astype(np.float64)) / 255.0
    ink = ink - float(ink.mean())
    window_y = np.hanning(rh + 2) if rh > 1 else np.ones(3)
    window_x = np.hanning(rw + 2) if rw > 1 else np.ones(3)
    window = window_y[1:-1, None] * window_x[None, 1:-1]
    spectrum = np.fft.fftshift(np.fft.fft2(ink * window))
    power = np.abs(spectrum) ** 2

    frequencies_y = np.fft.fftshift(np.fft.fftfreq(rh, d=1.0))
    frequencies_x = np.fft.fftshift(np.fft.fftfreq(rw, d=1.0))
    grid_x, grid_y = np.meshgrid(frequencies_x, frequencies_y)

    fundamental = job.lpi / job.dpi
    theta = np.deg2rad(job.angle_deg)
    cosine = np.cos(theta)
    sine = np.sin(theta)
    targets = [
        (fundamental * cosine, fundamental * sine),
        (-fundamental * cosine, -fundamental * sine),
        (-fundamental * sine, fundamental * cosine),
        (fundamental * sine, -fundamental * cosine),
    ]
    radius = max(
        0.16 * fundamental,
        2.0 * max(1.0 / rw, 1.0 / rh),
    )
    neighborhood = np.zeros_like(power, dtype=bool)
    for target_x, target_y in targets:
        neighborhood |= (
            ((grid_x - target_x) / radius) ** 2
            + ((grid_y - target_y) / radius) ** 2
            <= 1.0
        )

    total_energy = float(power.sum())
    neighborhood_energy = float(power[neighborhood].sum())
    concentration = 0.0 if total_energy == 0.0 else neighborhood_energy / total_energy
    exceeds = concentration > job.moire_threshold
    above_nyquist = fundamental > 0.5
    return {
        "metric": "screen_frequency_neighborhood_concentration",
        "risk_index": float(concentration),
        "moire_risk_index": float(concentration),
        "threshold": job.moire_threshold,
        "exceeds_threshold": bool(exceeds),
        "above_nyquist": bool(above_nyquist),
        "moire_risk": bool(exceeds or above_nyquist),
        "screen_frequency_cycles_per_pixel": float(fundamental),
        "mesh_pitch_px": float(job.mesh_pitch_px),
        "neighborhood_radius_cycles_per_pixel": float(radius),
        "valid_region": [rx, ry, rw, rh],
        "valid_pixels": int(rw * rh),
    }


def resample_ratio_description(source_width: int, source_height: int, job: ValidatedJob) -> Dict[str, Any]:
    ratio_x = rational_scale(source_width, job.output_width)
    ratio_y = rational_scale(source_height, job.output_height)
    return {
        "x": f"{ratio_x.numerator}/{ratio_x.denominator}",
        "y": f"{ratio_y.numerator}/{ratio_y.denominator}",
        "deterministic": True,
    }
