"""Sliding-window tone fidelity and source attribution."""

from __future__ import annotations

from typing import Any

import numpy as np

from .diffusion import DiffusionResult
from .validation import InputBundle


def _integral(array: np.ndarray) -> np.ndarray:
    return np.pad(array.astype(np.float64), ((1, 0), (1, 0))).cumsum(axis=0).cumsum(axis=1)


def _window_sum(integral: np.ndarray, y: int, x: int, height: int, width: int) -> float:
    return float(
        integral[y + height, x + width]
        - integral[y, x + width]
        - integral[y + height, x]
        + integral[y, x]
    )


def _boundary_band(mask: np.ndarray, band_px: int) -> np.ndarray:
    boundary = np.zeros(mask.shape, dtype=bool)
    boundary[:-1, :] |= mask[:-1, :] != mask[1:, :]
    boundary[:, :-1] |= mask[:, :-1] != mask[:, 1:]
    if band_px <= 0:
        return boundary
    out = boundary.copy()
    for radius in range(1, band_px + 1):
        shifted = boundary.copy()
        shifted[radius:, :] |= boundary[:-radius, :]
        shifted[:-radius, :] |= boundary[radius:, :]
        shifted[:, radius:] |= boundary[:, :-radius]
        shifted[:, :-radius] |= boundary[:, radius:]
        out |= shifted
    return out


def _window_positions(
    bbox: list[int],
    window: int,
    stride: int,
    shape: tuple[int, int],
) -> list[tuple[int, int]]:
    x, y, width, height = bbox
    height_img, width_img = shape
    if window > min(height_img, width_img):
        window = max(3, min(height_img, width_img) - (1 - min(height_img, width_img) % 2))
        stride = max(1, window // 2)
    y0 = max(0, y - (window - 1))
    x0 = max(0, x - (window - 1))
    y1 = min(height_img, y + height)
    x1 = min(width_img, x + width)
    positions: list[tuple[int, int]] = []
    for yy in range(y0, max(y0 + 1, y1 - window + 1), stride):
        for xx in range(x0, max(x0 + 1, x1 - window + 1), stride):
            positions.append((yy, xx))
    if not positions:
        positions.append((max(0, min(y, height_img - window)), max(0, min(x, width_img - window))))
    return positions


def build_tone_report(
    bundle: InputBundle,
    baseline: DiffusionResult,
    final: DiffusionResult,
) -> dict[str, Any]:
    cfg = bundle.config["tone"]
    window = int(cfg["window_px"])
    if window % 2 == 0:
        window += 1
    stride = max(1, window // 2)
    tolerance = float(cfg["tolerance"])
    min_mask_fraction = float(cfg["min_mask_fraction"])
    max_bad_fraction = float(cfg["max_bad_window_fraction"])
    max_report = int(cfg["max_reported_windows"])
    boundary_band_px = int(cfg["boundary_band_px"])

    source_ink = 1.0 - bundle.gray.astype(np.float64) / 255.0
    boundary = _boundary_band(bundle.mask, boundary_band_px)
    boundary_integral = _integral(boundary)
    clip_integral = _integral(final.weight_clip_map)
    unallocated_integral = _integral(np.abs(final.unallocated_map))

    parts: list[dict[str, Any]] = []
    any_outside = False
    for label, spec in sorted(bundle.part_specs.items()):
        bbox = bundle.bbox_for_label(label)
        if bbox is None:
            continue
        one = (bundle.mask == label).astype(np.float64)
        mask_integral = _integral(one)
        source_integral = _integral(source_ink * one)
        baseline_integral = _integral(baseline.binary.astype(np.float64) * one)
        final_integral = _integral(final.binary.astype(np.float64) * one)
        boundary_integral_part = _integral(boundary & (bundle.mask == label))

        positions = _window_positions(bbox, window, stride, bundle.gray.shape).copy()
        bad_count = 0
        valid_count = 0
        valid_positions: list[tuple[int, int]] = []
        signed_values: list[float] = []
        abs_values: list[float] = []
        bad_windows: list[dict[str, Any]] = []
        total_density_effect = 0.0
        total_boundary_effect = 0.0
        total_kernel_effect = 0.0
        total_clipped = 0.0
        total_unallocated = 0.0

        for y, x in positions:
            area = window * window
            mask_count = _window_sum(mask_integral, y, x, window, window)
            if mask_count < min_mask_fraction * area or mask_count <= 0:
                continue
            valid_count += 1
            valid_positions.append((y, x))
            source_mean = _window_sum(source_integral, y, x, window, window) / mask_count
            baseline_mean = _window_sum(baseline_integral, y, x, window, window) / mask_count
            final_mean = _window_sum(final_integral, y, x, window, window) / mask_count
            signed_dev = source_mean - final_mean
            abs_dev = abs(signed_dev)
            signed_values.append(signed_dev)
            abs_values.append(abs_dev)

            boundary_sum = _window_sum(boundary_integral_part, y, x, window, window)
            boundary_ratio = float(min(1.0, boundary_sum / mask_count))
            density_component = baseline_mean - final_mean
            residual_component = source_mean - baseline_mean
            boundary_component = residual_component * boundary_ratio
            kernel_component = residual_component * (1.0 - boundary_ratio)
            total_density_effect += density_component
            total_boundary_effect += boundary_component
            total_kernel_effect += kernel_component
            clip_mass = _window_sum(clip_integral, y, x, window, window)
            unallocated_mass = _window_sum(unallocated_integral, y, x, window, window)
            total_clipped += clip_mass
            total_unallocated += unallocated_mass

            over = abs_dev > tolerance
            if over:
                bad_count += 1
            if over and len(bad_windows) < max_report:
                components = {
                    "density_correction": abs(density_component),
                    "mask_boundary": abs(boundary_component),
                    "kernel_clipping": abs(kernel_component),
                }
                primary = max(components, key=components.get)
                bad_windows.append(
                    {
                        "x": int(x),
                        "y": int(y),
                        "width": window,
                        "height": window,
                        "mask_pixels": int(mask_count),
                        "source_mean_ink": float(source_mean),
                        "baseline_mean_ink": float(baseline_mean),
                        "final_mean_ink": float(final_mean),
                        "signed_deviation": float(signed_dev),
                        "abs_deviation": float(abs_dev),
                        "primary_source": primary,
                        "components_signed": {
                            "density_correction": float(density_component),
                            "mask_boundary": float(boundary_component),
                            "kernel_clipping": float(kernel_component),
                        },
                        "components_abs": components,
                        "weight_clip_mass_in_window": float(clip_mass),
                        "unallocated_abs_residual_in_window": float(unallocated_mass),
                    }
                )

        if valid_count == 0:
            continue
        mean_signed = float(np.mean(signed_values))
        mean_abs = float(np.mean(abs_values))
        max_abs = float(np.max(abs_values))
        max_index = int(np.argmax(abs_values))
        bad_fraction = bad_count / valid_count
        within = bad_fraction <= max_bad_fraction
        any_outside = any_outside or (not within)
        parts.append(
            {
                "label": label,
                "name": spec.get("name"),
                "bbox_xywh": bbox,
                "window_px": window,
                "stride_px": stride,
                "valid_windows": valid_count,
                "bad_windows": bad_count,
                "bad_window_fraction": float(bad_fraction),
                "max_bad_window_fraction": max_bad_fraction,
                "tolerance": tolerance,
                "mean_signed_deviation": mean_signed,
                "mean_abs_deviation": mean_abs,
                "max_abs_deviation": max_abs,
                "max_abs_window_position_xy": [
                    int(valid_positions[max_index][1]),
                    int(valid_positions[max_index][0]),
                ],
                "within_tolerance": bool(within),
                "density_correction_component_mean": float(total_density_effect / valid_count),
                "mask_boundary_component_mean": float(total_boundary_effect / valid_count),
                "kernel_clipping_component_mean": float(total_kernel_effect / valid_count),
                "weight_clip_mass_total": float(total_clipped),
                "unallocated_abs_residual_total": float(total_unallocated),
                "bad_window_locations": bad_windows,
                "bad_window_locations_truncated": bool(bad_count > len(bad_windows)),
            }
        )

    return {
        "schema": "clock-halftone-tone-report/v1",
        "ok": bool(not any_outside),
        "tolerance": tolerance,
        "max_bad_window_fraction": max_bad_fraction,
        "comparison": "masked sliding-window mean of continuous ink versus binary dot occupancy",
        "attribution_policy": {
            "density_correction": "baseline window tone minus corrected window tone",
            "mask_boundary": "mask-edge fraction times source-versus-baseline residual",
            "kernel_clipping": "interior fraction times source-versus-baseline residual",
        },
        "parts": parts,
    }
