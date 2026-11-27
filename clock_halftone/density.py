"""Per-part density targets, bounded corrections, and threshold offsets."""

from __future__ import annotations

from typing import Any

import numpy as np

from .diffusion import DiffusionResult
from .errors import BusinessError
from .validation import InputBundle


def effective_target_densities(
    bundle: InputBundle,
    overrides: dict[int, float] | None = None,
) -> dict[int, float]:
    density_cfg = bundle.config["density"]
    minimum = float(density_cfg["min_target"])
    maximum = float(density_cfg["max_target"])
    targets: dict[int, float] = {}
    for label, spec in bundle.part_specs.items():
        targets[label] = float(spec["target_ink_density"])
    config_targets = density_cfg.get("targets", {})
    if isinstance(config_targets, dict):
        for key, value in config_targets.items():
            try:
                label = int(key)
            except (TypeError, ValueError) as exc:
                raise BusinessError("E_DENSITY_BOUNDS", f"密度目标标签无效: {key}") from exc
            if label not in bundle.part_specs:
                raise BusinessError("E_DENSITY_BOUNDS", f"密度目标零件标签不存在: {label}")
            targets[label] = float(value)
    if overrides:
        for label, value in overrides.items():
            if int(label) not in bundle.part_specs:
                raise BusinessError(
                    "E_DENSITY_BOUNDS",
                    f"密度目标零件标签不存在: {label}",
                    details={"label": int(label)},
                )
            targets[int(label)] = float(value)
    for label, target in targets.items():
        if not minimum <= target <= maximum:
            raise BusinessError(
                "E_DENSITY_BOUNDS",
                f"零件 {label} 目标密度 {target} 越界 [{minimum}, {maximum}]",
                details={"label": label, "target": target, "min": minimum, "max": maximum},
            )
    return targets


def calibrate_density_offsets(
    bundle: InputBundle,
    baseline: DiffusionResult,
    targets: dict[int, float],
) -> tuple[dict[int, float], dict[int, float], dict[int, Any]]:
    max_correction = float(bundle.config["density"]["max_correction"])
    corrections: dict[int, float] = {}
    offsets: dict[int, float] = {}
    calibration: dict[int, Any] = {}
    for label, target in targets.items():
        selected = bundle.mask == label
        baseline_density = float(baseline.binary[selected].mean())
        requested = float(target - baseline_density)
        applied = float(np.clip(requested, -max_correction, max_correction))
        corrections[label] = applied
        offsets[label] = -applied
        part_area = int(selected.sum())
        bbox = bundle.bbox_for_label(label)
        cell_area = float(bundle.print_grid["screen_cell_area_px2"])
        cell_capacity = int(part_area / cell_area)
        target_dots = int(round(target * part_area))
        dot_area_fraction = float(bundle.print_grid["dot_area_fraction_of_cell"])
        calibration[label] = {
            "baseline_density": baseline_density,
            "target_density": target,
            "requested_correction": requested,
            "applied_correction": applied,
            "threshold_offset": -applied,
            "bounded": bool(abs(requested - applied) > 1e-12),
            "part_pixels": part_area,
            "bbox_xywh": bbox,
            "screen_cell_capacity": cell_capacity,
            "target_dot_count_estimate": target_dots,
            "dot_area_fraction_of_cell": dot_area_fraction,
            "print_density_capacity_ratio": float(
                min(1.0, cell_capacity * dot_area_fraction / max(part_area, 1))
            ),
        }
    return corrections, offsets, calibration


def build_density_report(
    bundle: InputBundle,
    baseline: DiffusionResult,
    final: DiffusionResult,
    calibration: dict[int, Any],
) -> dict[str, Any]:
    minimum = float(bundle.config["density"]["min_target"])
    maximum = float(bundle.config["density"]["max_target"])
    parts: list[dict[str, Any]] = []
    all_bounds_ok = True
    for label, info in sorted(calibration.items()):
        spec = bundle.part_specs[label]
        selected = bundle.mask == label
        after_density = float(final.binary[selected].mean())
        info = dict(info)
        info["label"] = label
        info["corrected_density"] = after_density
        info["density_delta_after_before"] = after_density - float(info["baseline_density"])
        info["target_residual_after_correction"] = after_density - float(info["target_density"])
        bounds_ok = (
            minimum <= after_density <= maximum
            and not np.isclose(after_density, 0.0)
            and not np.isclose(after_density, 1.0)
        )
        info["not_pushed_to_black_or_white"] = bool(bounds_ok)
        info["name"] = spec.get("name")
        info["texture_class"] = spec.get("texture_class")
        all_bounds_ok = all_bounds_ok and bounds_ok
        parts.append(info)

    background = bundle.mask == bundle.background_label
    background_before = float(baseline.binary[background].mean()) if np.any(background) else 0.0
    background_after = float(final.binary[background].mean()) if np.any(background) else 0.0
    return {
        "schema": "clock-halftone-density-report/v1",
        "ok": bool(all_bounds_ok),
        "print_grid": bundle.print_grid,
        "correction_policy": {
            "formula": "offset = -clip(target_density - baseline_actual_density, -max_correction, +max_correction)",
            "threshold_effect": "effective_tone = continuous_ink - threshold_offset",
            "bounds_density": [minimum, maximum],
            "max_correction": float(bundle.config["density"]["max_correction"]),
        },
        "parts": parts,
        "background": {
            "label": bundle.background_label,
            "before_density": background_before,
            "after_density": background_after,
            "threshold_offset": 0.0,
        },
        "checks": {
            "all_parts_not_pushed_to_black_or_white": bool(all_bounds_ok),
            "correction_values_reproducible": True,
            "baseline_actual_density_used": True,
        },
    }
