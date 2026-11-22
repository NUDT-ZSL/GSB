"""Stage-isolated error propagation, intervals, amplification and attribution."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .errors import ErrorSpectrumInvalid
from .utils import rms

CONTROLLED_LEVELS = (
    "window_boundary",
    "feature_scaling",
    "chunk_gradient_accumulation",
    "fixed_point_quantization",
    "recursive_prediction",
)
REPORT_FLOOR = 1e-12


def _window_steps(
    forecast: Mapping[str, Any],
) -> dict[str, list[float]]:
    return {
        str(window["window_id"]): [
            float(step["prediction"]) for step in window["steps"]
        ]
        for window in forecast["windows"]
    }


def _window_rmse(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("window forecast lengths differ")
    return rms(
        float(a) - float(b) for a, b in zip(left, right)
    )


def build_error_spectrum(
    *,
    reference: Mapping[str, Any],
    feature_counterfactual: Mapping[str, Any],
    actual: Mapping[str, Any],
    open_loop_actual: Mapping[str, Any],
    pre_model_quantization: Mapping[str, Any],
    precision_profile: str,
) -> dict[str, Any]:
    """Build isolated stage errors plus an explicit total-vs-reference view.

    Stage 3 isolates chunked model training from the reference model while
    retaining the executed feature boundary. Stage 4 is nonzero only for the
    fixed-point profile and isolates model-parameter quantization. Stage 5
    isolates prediction feedback by comparing recursive and open-loop forecasts
    that use the same final model and feature vectors.
    """

    reference_map = _window_steps(reference)
    feature_map = _window_steps(feature_counterfactual)
    actual_map = _window_steps(actual)
    open_loop_map = _window_steps(open_loop_actual)
    pre_quant_map = _window_steps(pre_model_quantization)
    window_ids = sorted(reference_map)
    missing: set[str] = set()
    required = set(window_ids)
    for stage_map in (
        feature_map,
        actual_map,
        open_loop_map,
        pre_quant_map,
    ):
        missing |= required - set(stage_map)
    if missing:
        first = sorted(missing)[0]
        raise ErrorSpectrumInvalid(
            "a forecast stage is missing a test window",
            window_id=first,
            level="recursive_prediction",
        )

    stage_inputs = {
        "window_boundary": (reference_map, reference_map),
        "feature_scaling": (feature_map, reference_map),
        "chunk_gradient_accumulation": (actual_map, feature_map),
        "fixed_point_quantization": (
            (actual_map, pre_quant_map)
            if precision_profile == "fixed_point"
            else (pre_quant_map, pre_quant_map)
        ),
        "recursive_prediction": (actual_map, open_loop_map),
    }
    isolates = {
        "window_boundary": "window IDs, boundaries and row order",
        "feature_scaling": "profile quantization at scaled feature boundary",
        "chunk_gradient_accumulation": (
            "chunked model training relative to reference-model features"
        ),
        "fixed_point_quantization": (
            "fixed-point model parameters relative to pre-quantization model"
        ),
        "recursive_prediction": "recursive feedback relative to open-loop truth",
    }

    levels: list[dict[str, Any]] = []
    previous_absolute = None
    for level in CONTROLLED_LEVELS:
        value_map, baseline_map = stage_inputs[level]
        deviations: list[dict[str, Any]] = []
        for window_id in window_ids:
            absolute = _window_rmse(
                value_map[window_id],
                baseline_map[window_id],
            )
            baseline_rms = rms(baseline_map[window_id])
            relative = absolute / max(baseline_rms, REPORT_FLOOR)
            deviations.append(
                {
                    "window_id": window_id,
                    "window_index": next(
                        int(window["window_index"])
                        for window in reference["windows"]
                        if str(window["window_id"]) == window_id
                    ),
                    "absolute_deviation_rms": float(absolute),
                    "relative_deviation": float(relative),
                    "baseline_rms": float(baseline_rms),
                }
            )
        absolute_error = rms(
            deviation["absolute_deviation_rms"] for deviation in deviations
        )
        baseline_aggregate = rms(
            deviation["baseline_rms"] for deviation in deviations
        )
        relative_error = absolute_error / max(baseline_aggregate, REPORT_FLOOR)
        interval_lower = min(
            (deviation["absolute_deviation_rms"] for deviation in deviations),
            default=0.0,
        )
        interval_upper = max(
            (deviation["absolute_deviation_rms"] for deviation in deviations),
            default=0.0,
        )
        amplification = (
            1.0
            if previous_absolute is None
            else absolute_error / max(previous_absolute, REPORT_FLOOR)
        )
        levels.append(
            {
                "level": level,
                "isolation": isolates[level],
                "aggregation": "rms_of_window_rms",
                "absolute_error": float(absolute_error),
                "relative_error": float(relative_error),
                "reference_rms": float(baseline_aggregate),
                "interval_lower": float(interval_lower),
                "interval_upper": float(interval_upper),
                "interval_kind": "min_max_window_rms",
                "amplification_ratio": float(amplification),
                "deviations": deviations,
            }
        )
        previous_absolute = absolute_error

    dominant = max(
        levels,
        key=lambda item: item["absolute_error"],
    )
    dominant_source = (
        "none"
        if float(dominant["absolute_error"]) <= REPORT_FLOOR
        else str(dominant["level"])
    )
    total_deviations: list[dict[str, Any]] = []
    for window_id in window_ids:
        absolute = _window_rmse(actual_map[window_id], reference_map[window_id])
        reference_rms = rms(reference_map[window_id])
        total_deviations.append(
            {
                "window_id": window_id,
                "absolute_deviation_rms": float(absolute),
                "relative_deviation": float(
                    absolute / max(reference_rms, REPORT_FLOOR)
                ),
                "reference_rms": float(reference_rms),
            }
        )
    total_absolute = rms(
        item["absolute_deviation_rms"] for item in total_deviations
    )
    spectrum = {
        "reference": "float64 full-batch recursive forecast",
        "precision_profile": precision_profile,
        "controlled_levels": list(CONTROLLED_LEVELS),
        "levels": levels,
        "dominant_source": dominant_source,
        "dominant_absolute_error": float(dominant["absolute_error"]),
        "total_vs_reference": {
            "aggregation": "rms_of_window_rms",
            "absolute_error": float(total_absolute),
            "interval_lower": min(
                (
                    item["absolute_deviation_rms"]
                    for item in total_deviations
                ),
                default=0.0,
            ),
            "interval_upper": max(
                (
                    item["absolute_deviation_rms"]
                    for item in total_deviations
                ),
                default=0.0,
            ),
            "deviations": total_deviations,
        },
    }
    validate_error_spectrum(spectrum)
    return spectrum


def validate_error_spectrum(spectrum: Mapping[str, Any]) -> None:
    levels = spectrum.get("levels")
    if not isinstance(levels, list) or not levels:
        raise ErrorSpectrumInvalid(
            "error spectrum has no levels",
            level=None,
        )
    seen: set[str] = set()
    for level in levels:
        name = level.get("level")
        if name not in CONTROLLED_LEVELS:
            raise ErrorSpectrumInvalid(
                f"uncontrolled error-spectrum level: {name!r}",
                level=str(name) if name is not None else None,
                window_index=(
                    int(level["deviations"][0]["window_index"])
                    if level.get("deviations")
                    else None
                ),
            )
        seen.add(str(name))
        lower = float(level.get("interval_lower", 0.0))
        upper = float(level.get("interval_upper", 0.0))
        if not math.isfinite(lower) or not math.isfinite(upper) or upper < lower:
            first_window = (
                level["deviations"][0]
                if level.get("deviations")
                else {}
            )
            raise ErrorSpectrumInvalid(
                "error interval upper bound is below lower bound",
                window_index=first_window.get("window_index"),
                window_id=first_window.get("window_id"),
                level=str(name),
            )
        for deviation in level.get("deviations", []):
            absolute = float(deviation.get("absolute_deviation_rms", 0.0))
            relative = float(deviation.get("relative_deviation", 0.0))
            if not math.isfinite(absolute) or not math.isfinite(relative):
                raise ErrorSpectrumInvalid(
                    "error deviation is not finite",
                    window_index=deviation.get("window_index"),
                    window_id=deviation.get("window_id"),
                    level=str(name),
                )
    if seen != set(CONTROLLED_LEVELS):
        missing = sorted(set(CONTROLLED_LEVELS) - seen)
        raise ErrorSpectrumInvalid(
            f"missing controlled error-spectrum levels: {missing}",
            level=missing[0],
        )
