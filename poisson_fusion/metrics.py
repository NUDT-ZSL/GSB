"""Seam and gradient-fidelity metrics tied directly to saved error maps."""

from __future__ import annotations

from typing import Any

import numpy as np

from .field_builder import FieldBundle
from .geometry import inner_boundary, make_band


def quantize_rgb(image: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(np.asarray(image, dtype=np.float64)), 0, 255).astype(np.uint8)


def seam_error_rgb(fused_u8: np.ndarray, base_u8: np.ndarray, band: np.ndarray) -> np.ndarray:
    error = np.abs(fused_u8.astype(np.float32) - base_u8.astype(np.float32))
    error[~band] = 0.0
    return error.astype(np.float32)


def seam_error_scalar(error_rgb: np.ndarray, band: np.ndarray) -> np.ndarray:
    scalar = np.max(error_rgb, axis=2).astype(np.float32)
    scalar[~band] = 0.0
    return scalar


def gradient_error_map(
    fused_u8: np.ndarray,
    bundle: FieldBundle,
    band: np.ndarray,
) -> np.ndarray:
    """Per-pixel RMS vector error against the serialized foreground gradients."""

    fused = fused_u8.astype(np.float64)
    maps = np.zeros(fused.shape[:2], dtype=np.float64)
    counts = np.zeros(fused.shape[:2], dtype=np.float64)

    gx_fused = np.diff(fused, axis=1)
    gx_target = bundle.gradient_x[:, :-1, :]
    gx_error = np.sqrt(np.mean((gx_fused - gx_target) ** 2, axis=2))
    band_x = band[:, :-1] | band[:, 1:]
    valid_x = band_x & (bundle.boundary_mask[:, :-1] | bundle.unknown_mask[:, :-1])
    valid_x &= bundle.boundary_mask[:, 1:] | bundle.unknown_mask[:, 1:]
    maps[:, :-1] += np.where(valid_x, gx_error, 0.0)
    maps[:, 1:] += np.where(valid_x, gx_error, 0.0)
    counts[:, :-1] += valid_x.astype(np.float64)
    counts[:, 1:] += valid_x.astype(np.float64)

    gy_fused = np.diff(fused, axis=0)
    gy_target = bundle.gradient_y[:-1, :, :]
    gy_error = np.sqrt(np.mean((gy_fused - gy_target) ** 2, axis=2))
    band_y = band[:-1, :] | band[1:, :]
    valid_y = band_y & (bundle.boundary_mask[:-1, :] | bundle.unknown_mask[:-1, :])
    valid_y &= bundle.boundary_mask[1:, :] | bundle.unknown_mask[1:, :]
    maps[:-1, :] += np.where(valid_y, gy_error, 0.0)
    maps[1:, :] += np.where(valid_y, gy_error, 0.0)
    counts[:-1, :] += valid_y.astype(np.float64)
    counts[1:, :] += valid_y.astype(np.float64)

    result = np.zeros_like(maps)
    nonzero = counts > 0
    result[nonzero] = maps[nonzero] / counts[nonzero]
    result[~band] = 0.0
    return result.astype(np.float32)


def _values_under(mask: np.ndarray, region: np.ndarray) -> np.ndarray:
    return np.asarray(mask[region], dtype=np.float64)


def _summary(
    values: np.ndarray,
    *,
    include_percentiles: bool,
) -> dict[str, Any]:
    if values.size == 0:
        return {"count": 0}
    exact = values.astype(np.float64)
    summary: dict[str, Any] = {
        "count": int(values.size),
        "max": float(np.max(exact)),
        "mean": float(np.mean(exact)),
        "min": float(np.min(exact)),
    }
    if include_percentiles:
        summary.update(
            {
                "p50": float(np.percentile(exact, 50)),
                "p90": float(np.percentile(exact, 90)),
                "p95": float(np.percentile(exact, 95)),
                "p99": float(np.percentile(exact, 99)),
            }
        )
    return summary


def _max_location(map_array: np.ndarray, region: np.ndarray) -> list[int] | None:
    values = np.where(region, map_array, -np.inf)
    if not np.isfinite(values).any():
        return None
    y, x = np.unravel_index(int(np.argmax(values)), values.shape)
    return [int(y), int(x)]


def build_metrics(
    fused_u8: np.ndarray,
    base_u8: np.ndarray,
    bundle: FieldBundle,
    *,
    band_radius: int,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    boundary = inner_boundary(bundle.unknown_mask | bundle.boundary_mask)
    band = make_band(boundary, band_radius)
    rgb_error = seam_error_rgb(fused_u8, base_u8, band)
    scalar_error = seam_error_scalar(rgb_error, band)
    grad_error = gradient_error_map(fused_u8, bundle, band)
    ring_error = np.max(
        np.abs(fused_u8.astype(np.int16) - base_u8.astype(np.int16))[boundary],
        axis=1,
    ) if boundary.any() else np.zeros(0, dtype=np.int16)

    seam_values = _values_under(scalar_error, band)
    gradient_values = _values_under(grad_error, band)
    interior_values = _values_under(grad_error, bundle.unknown_mask)
    report = {
        "units": "8-bit quantization levels",
        "band": {
            "radius": int(band_radius),
            "definition": "4-neighbour Manhattan dilation of the inner mask boundary",
            "pixels": int(np.count_nonzero(band)),
            "inside_mask_pixels": int(np.count_nonzero(band & (bundle.unknown_mask | bundle.boundary_mask))),
        },
        "seam_error": {
            **_summary(seam_values, include_percentiles=True),
            "max_location": _max_location(scalar_error, band),
            "nonzero_pixels": int(np.count_nonzero(scalar_error[band] > 0)),
            "error_map": "seam_error_map.npy",
            "rgb_error_map": "seam_error_rgb.npy",
            "description": "max absolute RGB difference between quantized fused output and base",
        },
        "gradient_error": {
            **_summary(gradient_values, include_percentiles=True),
            "max_location": _max_location(grad_error, band),
            "error_map": "gradient_error_map.npy",
            "description": "RMS error against serialized foreground gradient on valid mask edges",
        },
        "interior_gradient_error": {
            **_summary(interior_values, include_percentiles=True),
            "max_location": _max_location(grad_error, bundle.unknown_mask),
            "description": "gradient error on interior (non-boundary) mask pixels",
        },
        "boundary_match": {
            "boundary_pixels": int(np.count_nonzero(boundary)),
            "max_error": float(np.max(ring_error)) if ring_error.size else 0.0,
            "mean_error": float(np.mean(ring_error)) if ring_error.size else 0.0,
            "pixels_over_one_level": int(np.count_nonzero(ring_error > 1)),
        },
        "consistency": {
            "seam_stats_source": "seam_error_map.npy restricted to boundary_band",
            "gradient_stats_source": "gradient_error_map.npy restricted to boundary_band",
            "numeric_precision": "float64 statistics computed directly from float32 error maps",
        },
    }
    arrays = {
        "seam_error_rgb": rgb_error,
        "seam_error_map": scalar_error,
        "gradient_error_map": grad_error,
        "fusion_band": band.astype(np.uint8),
    }
    return report, arrays
