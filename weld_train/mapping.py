"""Intensity mapping curves and source-category parsing."""

from __future__ import annotations

import copy

import numpy as np


def validate_project_tables(project: dict) -> tuple[dict, dict]:
    """Return category and mapping-curve dictionaries after schema checks."""
    categories = {}
    for category in project.get("source_categories", []):
        source_type = category.get("source_type")
        if not source_type:
            raise ValueError("source category is missing source_type")
        if source_type in categories:
            raise ValueError(f"duplicate source category: {source_type}")
        categories[source_type] = category

    curves = {}
    for curve in project.get("intensity_mapping_curves", []):
        curve_id = curve.get("id")
        if not curve_id:
            raise ValueError("mapping curve is missing id")
        if curve_id in curves:
            raise ValueError(f"duplicate mapping curve: {curve_id}")
        x = np.asarray(curve.get("x", []), dtype=float)
        y = np.asarray(curve.get("y", []), dtype=float)
        if x.size < 2 or y.size != x.size:
            raise ValueError(f"mapping curve {curve_id} must have equal x/y arrays")
        if np.any(np.diff(x) <= 0):
            raise ValueError(f"mapping curve {curve_id} x values must increase")
        curves[curve_id] = curve

    for source_type, category in categories.items():
        mapping_id = category.get("mapping_id")
        if mapping_id not in curves:
            raise ValueError(
                f"source category {source_type} references missing map {mapping_id}"
            )
    return categories, curves


def evaluate_mapping(curve: dict, driver: float | np.ndarray) -> np.ndarray:
    """Piecewise-linear interpolation with constant extrapolation."""
    x = np.asarray(curve["x"], dtype=float)
    y = np.asarray(curve["y"], dtype=float)
    values = np.interp(np.asarray(driver, dtype=float), x, y, left=y[0], right=y[-1])
    values = np.maximum(values, 0.0)
    if np.isscalar(driver):
        return float(values)
    return values


def mapping_parameters(
    category: dict,
    curve: dict,
    segment: dict,
    event: dict | None = None,
) -> dict:
    """Serializable mapping parameters attached to each source object."""
    parameters = {
        "mapping_id": curve["id"],
        "curve_kind": curve.get("kind", "piecewise_linear"),
        "input": curve.get("input"),
        "x": copy.deepcopy(curve["x"]),
        "y": copy.deepcopy(curve["y"]),
        "base_level_db": float(category.get("base_level_db", 0.0)),
        "reference_distance_m": float(category.get("reference_distance_m", 0.5)),
        "distance_model": "clamped_inverse_amplitude",
        "power_normalization": "constant_power",
        "driver_reference": copy.deepcopy(category.get("driver_reference", {})),
        "seed": int(segment.get("seed", 0)),
    }
    if event is not None:
        parameters.update(
            {
                "event_id": event["event_id"],
                "event_strength_scale": float(event["strength_scale"]),
                "decay_rate_hz": float(event["decay_rate_hz"]),
                "envelope": "attack_then_exponential_decay",
            }
        )
    return parameters
