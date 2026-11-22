"""Future-value-isolated recursive and diagnostic open-loop forecasting."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

from .errors import RecursionInvalid
from .evaluation import threshold_decision
from .models import Model
from .precision import Quantizer
from .scaling import FeatureScaler
from .series import SeriesData
from .utils import guard_subnormal, sha256_json
from .windows import Window, WindowCollection, build_feature_row_from_history


def _predict_steps(
    model: Model,
    window: Window,
    series: SeriesData,
    scaler: FeatureScaler,
    config: Mapping[str, Any],
    feature_quantizer: Quantizer,
    *,
    mode: str,
    requested_steps: int | None,
) -> list[dict[str, Any]]:
    horizon = int(config["window"]["horizon"])
    steps_to_run = horizon if requested_steps is None else int(requested_steps)
    if steps_to_run <= 0 or steps_to_run > horizon:
        invalid_step = steps_to_run if steps_to_run > horizon else 1
        raise RecursionInvalid(
            f"requested recursion depth {steps_to_run} violates horizon {horizon}",
            window_index=window.window_index,
            window_id=window.window_id,
            step=invalid_step,
        )
    if mode not in {"recursive", "open_loop_diagnostic"}:
        raise ValueError(f"unsupported forecast mode: {mode}")

    history = [
        float(value)
        for value in series.target[window.history_start : window.history_end]
    ]
    observed_origin = window.history_start
    predictions: list[float] = []
    trace: list[dict[str, Any]] = []
    max_true_target_index_accessed = window.target_start - 1
    for step in range(1, steps_to_run + 1):
        anchor = window.history_end - 1 + (step - 1)
        if mode == "open_loop_diagnostic":
            history = [
                float(value)
                for value in series.target[observed_origin : anchor + 1]
            ]
            max_true_target_index_accessed = max(
                max_true_target_index_accessed,
                anchor,
            )
        raw_features = build_feature_row_from_history(
            history,
            observed_origin,
            anchor,
            series,
            config,
        )
        scaled_features = scaler.transform_row(raw_features)
        executed_features = feature_quantizer.quantize_array(scaled_features)
        prediction = float(model.predict_one(executed_features))
        margin = (
            float(config["evaluation"]["confidence_z"])
            * float(model.residual_std)
            * math.sqrt(float(step))
        )
        trace.append(
            {
                "step": step,
                "anchor_index": anchor,
                "predicted_target_index": anchor + 1,
                "feature_columns": list(window.feature_columns),
                "raw_features": [float(value) for value in raw_features],
                "scaled_features": [float(value) for value in scaled_features],
                "executed_features": [float(value) for value in executed_features],
                "prediction": prediction,
                "interval_lower": float(prediction - margin),
                "interval_upper": float(prediction + margin),
                "confidence_z": float(config["evaluation"]["confidence_z"]),
                "max_true_target_index_accessed": max_true_target_index_accessed,
                "uses_prior_prediction": bool(step > 1 and mode == "recursive"),
                "mode": mode,
            }
        )
        predictions.append(prediction)
        history.append(prediction)

    if mode == "recursive" and max_true_target_index_accessed >= window.target_start:
        raise RecursionInvalid(
            "recursive prediction accessed a future target value",
            window_index=window.window_index,
            window_id=window.window_id,
            step=1,
        )
    return trace


def forecast_windows(
    model: Model,
    collection: WindowCollection,
    series: SeriesData,
    scaler: FeatureScaler,
    config: Mapping[str, Any],
    feature_quantizer: Quantizer | None = None,
    *,
    mode: str = "recursive",
    requested_steps: int | None = None,
) -> dict[str, Any]:
    """Forecast each test window; future truths are attached only afterwards."""

    quantizer = feature_quantizer or Quantizer.identity()
    windows_payload: list[dict[str, Any]] = []
    all_steps: list[dict[str, Any]] = []
    guard = bool(config["training"]["subnormal_guard"])
    for window in collection.test_windows:
        raw_trace = _predict_steps(
            model,
            window,
            series,
            scaler,
            config,
            quantizer,
            mode=mode,
            requested_steps=requested_steps,
        )
        steps: list[dict[str, Any]] = []
        cumulative_abs_error = 0.0
        for trace in raw_trace:
            true_value = float(
                series.target[int(trace["predicted_target_index"])]
            )
            error = float(trace["prediction"]) - true_value
            error = guard_subnormal(error, guard)
            absolute_error = abs(error)
            cumulative_abs_error += absolute_error
            decision = threshold_decision(absolute_error, config)
            record = {
                **trace,
                "true_value": true_value,
                "true_value_use": "evaluation_only_after_prediction",
                "error": error,
                "absolute_error": float(absolute_error),
                "relative_error": float(
                    absolute_error
                    / max(abs(true_value), float(config["evaluation"]["metric_epsilon"]))
                ),
                "cumulative_absolute_error": float(cumulative_abs_error),
                "decision": decision,
            }
            steps.append(record)
            all_steps.append(record)
        windows_payload.append(
            {
                "window_id": window.window_id,
                "window_index": window.window_index,
                "start_index": window.start_index,
                "history_bounds": [window.history_start, window.history_end],
                "target_bounds": [window.target_start, window.target_end],
                "steps": steps,
                "prediction_sha256": sha256_json(
                    [step["prediction"] for step in steps]
                ),
                "max_true_target_index_accessed": max(
                    step["max_true_target_index_accessed"] for step in steps
                ),
            }
        )
    return {
        "mode": mode,
        "window_count": len(windows_payload),
        "step_count": len(all_steps),
        "windows": windows_payload,
        "steps_flat": all_steps,
        "prediction_sha256": sha256_json(
            [
                [step["prediction"] for step in window["steps"]]
                for window in windows_payload
            ]
        ),
    }


def forecast_step_map(forecast: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        str(window["window_id"]): list(window["steps"])
        for window in forecast["windows"]
    }


def prediction_digest(forecast: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            str(window["window_id"]): [
                float(step["prediction"]) for step in window["steps"]
            ]
            for window in forecast["windows"]
        }
    )
