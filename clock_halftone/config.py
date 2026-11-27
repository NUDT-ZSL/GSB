"""Configuration loading and conservative defaults."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .errors import BusinessError


DEFAULT_CONFIG: dict[str, Any] = {
    "seed": 20261128,
    "print": {
        "lines_per_inch": 50.0,
        "ink_dot_diameter_mm": 0.15,
        "board_width_mm": 86.36,
        "board_height_mm": 86.36,
        "aspect_tolerance": 0.03,
        "dot_to_cell_tolerance": 0.03,
    },
    "dither": {
        "seed": 7312026,
        "texture_amplitudes": {
            "flat": 0.110,
            "spiral": 0.028,
            "tooth": 0.075,
            "bore": 0.060,
        },
    },
    "error_diffusion": {
        "serpentine": True,
        "gradient_low": 0.012,
        "gradient_high": 0.090,
        "direction_strength": 1.55,
        "base_weights": [7.0, 3.0, 5.0, 1.0],
        "min_kernel_weight": 0.0,
    },
    "density": {
        "min_target": 0.035,
        "max_target": 0.965,
        "max_correction": 0.20,
        "targets": {},
    },
    "tone": {
        "window_px": 16,
        "tolerance": 0.055,
        "max_bad_window_fraction": 0.08,
        "min_mask_fraction": 0.25,
        "max_reported_windows": 200,
        "boundary_band_px": 2,
    },
    "spectrum": {
        "min_peak_ratio": 4.5,
        "peak_exclusion_radius_px": 3,
        "min_frequency": 0.035,
        "max_frequency": 0.48,
        "screen_frequency_tolerance": 0.10,
        "tooth_frequency_tolerance": 0.18,
        "band_z_threshold": 1.75,
        "max_bands": 24,
        "max_peaks": 12,
        "max_strong_screen_peaks_before_fail": 3,
        "max_risk_score_before_fail": 6.0,
    },
    "detail": {
        "edge_tone": 0.42,
        "solid_threshold": 0.44,
        "smoothing_cell_factor": 2.0,
        "smoothing_dot_factor": 3.0,
        "min_smoothing_px": 1,
        "max_smoothing_px": 19,
        "max_hole_fill_fraction_of_smoothing_area": 1.00,
        "edge_tolerance_dot_radii": 0.90,
        "edge_tolerance_min_px": 1.5,
        "edge_p95_tolerance_multiple": 4.0,
        "bore_open_max_ink": 0.20,
        "root_gap_min_px": 1.0,
        "max_raw_dot_components_report": 5000,
    },
    "validation": {
        "expected_bit_depth": 8,
        "allowed_texture_classes": ["flat", "spiral", "tooth", "bore"],
        "accept_mask_missing_label": -1,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if path is None:
        return config
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BusinessError(
            "E_CONFIG", f"配置文件不存在: {config_path}", details={"path": str(config_path)}
        ) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "E_CONFIG",
            f"配置文件不是合法 JSON: {exc}",
            details={"path": str(config_path)},
        ) from exc
    if not isinstance(raw, dict):
        raise BusinessError("E_CONFIG", "配置文件根节点必须是对象")
    return _deep_merge(config, raw)


def validate_config(config: dict[str, Any]) -> None:
    def number(section: str, key: str, *, low: float | None = None, high: float | None = None) -> float:
        value = config.get(section, {}).get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BusinessError("E_CONFIG", f"{section}.{key} 必须是数值")
        value = float(value)
        if low is not None and value < low:
            raise BusinessError("E_CONFIG", f"{section}.{key} 必须 >= {low}")
        if high is not None and value > high:
            raise BusinessError("E_CONFIG", f"{section}.{key} 必须 <= {high}")
        return value

    for section in (
        "print",
        "dither",
        "error_diffusion",
        "density",
        "tone",
        "spectrum",
        "detail",
        "validation",
    ):
        if not isinstance(config.get(section), dict):
            raise BusinessError("E_CONFIG", f"缺少配置段 {section}")

    number("print", "lines_per_inch", low=1.0)
    number("print", "ink_dot_diameter_mm", low=0.0)
    number("print", "board_width_mm", low=0.0)
    number("print", "board_height_mm", low=0.0)
    number("error_diffusion", "gradient_low", low=0.0)
    number("error_diffusion", "gradient_high", low=0.0)
    if config["error_diffusion"]["gradient_high"] <= config["error_diffusion"]["gradient_low"]:
        raise BusinessError("E_CONFIG", "gradient_high 必须大于 gradient_low")
    number("error_diffusion", "direction_strength", low=0.0)
    base = config["error_diffusion"].get("base_weights")
    if not isinstance(base, list) or len(base) != 4 or any(float(x) < 0 for x in base) or sum(base) <= 0:
        raise BusinessError("E_CONFIG", "error_diffusion.base_weights 必须是 4 个非负数且总和大于 0")
    number("density", "min_target", low=0.0, high=1.0)
    number("density", "max_target", low=0.0, high=1.0)
    if config["density"]["min_target"] >= config["density"]["max_target"]:
        raise BusinessError("E_CONFIG", "density.min_target 必须小于 max_target")
    number("density", "max_correction", low=0.0, high=1.0)
    number("tone", "window_px", low=3.0)
    number("tone", "tolerance", low=0.0, high=1.0)
    number("detail", "edge_tone", low=0.0, high=1.0)
    amplitudes = config.get("dither", {}).get("texture_amplitudes")
    if not isinstance(amplitudes, dict):
        raise BusinessError("E_CONFIG", "dither.texture_amplitudes 必须是对象")
    for texture in config["validation"]["allowed_texture_classes"]:
        value = amplitudes.get(texture)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 0.5:
            raise BusinessError("E_CONFIG", f"dither.texture_amplitudes.{texture} 必须在 [0, 0.5]")
    for key, value in config["density"].get("targets", {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BusinessError("E_CONFIG", f"density.targets.{key} 必须是数值")
        if not 0.0 <= float(value) <= 1.0:
            raise BusinessError("E_CONFIG", f"density.targets.{key} 必须在 [0, 1]")
