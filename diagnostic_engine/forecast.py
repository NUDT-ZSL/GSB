"""Stage 4: explicit linear quantile prediction, errors and drift."""

from __future__ import annotations

from typing import Any

import numpy as np

from .errors import drift_missing, quantile_order
from .precision import Precision


def validate_quantile_levels(levels: list[float]) -> None:
    if not levels or any(not 0.0 < float(q) < 1.0 for q in levels):
        raise quantile_order("quantile levels must lie in (0, 1)", quantiles=levels)
    if any(float(a) >= float(b) for a, b in zip(levels, levels[1:])):
        raise quantile_order("quantile levels must be strictly ascending", quantiles=levels)


def predict_quantiles(
    coordinates: np.ndarray,
    model: dict[str, Any],
    precision: Precision,
) -> dict[str, Any]:
    """Independent prediction primitive using only coordinates and coefficients."""
    z = np.asarray(coordinates, dtype=np.float64)
    coefficients = np.asarray(model["coefficients"], dtype=np.float64)
    intercepts = np.asarray(model["intercepts"], dtype=np.float64)
    levels = list(model["quantiles"])
    validate_quantile_levels(levels)
    if coefficients.shape[0] != len(levels) or coefficients.shape[1] != z.shape[1]:
        raise quantile_order(
            "predictor coefficient shape does not match quantiles/latent dimension",
            coefficient_shape=list(coefficients.shape),
            latent_dim=z.shape[1],
            quantile_count=len(levels),
        )
    if intercepts.shape[0] != len(levels):
        raise quantile_order("intercept count does not match quantiles")

    raw_predictions = precision.add(
        precision.matmul(z, coefficients.T),
        intercepts[None, :],
    )
    predictions = precision.q(raw_predictions)
    violations = []
    for row in range(predictions.shape[0]):
        for col in range(1, predictions.shape[1]):
            if predictions[row, col] < predictions[row, col - 1]:
                violations.append({"row": row, "lower": col - 1, "upper": col})
    if violations:
        raise quantile_order("predicted quantiles are not ordered", violations=violations[:8])
    return {
        "quantiles": levels,
        "predictions": predictions,
        "coefficients": coefficients,
        "intercepts": intercepts,
        "precision": precision.config_view(),
    }


def _standardized_target(preprocess_result: dict[str, Any], target_var: str, precision: Precision) -> np.ndarray:
    variables = preprocess_result["variables"]
    if target_var not in variables:
        raise drift_missing("predictor target variable is unavailable", target_var=target_var)
    index = variables.index(target_var)
    target = np.asarray(preprocess_result["matrix"], dtype=np.float64)[:, index]
    stat = preprocess_result["stats"][target_var]
    mean = np.float64(stat["mean"])
    std = np.float64(stat["std"])
    if std == 0.0:
        from .errors import std_zero

        raise std_zero("prediction target standard deviation is zero", target_var=target_var)
    return precision.div(precision.sub(target, mean), std)


def predict_window(
    reduction_result: dict[str, Any],
    preprocess_result: dict[str, Any],
    config: dict[str, Any],
    precision: Precision,
) -> dict[str, Any]:
    predictor_cfg = dict(config["predictor"])
    horizon = int(predictor_cfg.get("horizon_rows", 1))
    coordinates = np.asarray(reduction_result["coordinates"], dtype=np.float64)
    if horizon < 1:
        from .errors import bad_input

        raise bad_input("horizon_rows must be positive", horizon_rows=horizon)
    if coordinates.shape[0] <= horizon:
        raise drift_missing("not enough rows for one-step forecast", rows=coordinates.shape[0], horizon=horizon)

    target = _standardized_target(preprocess_result, predictor_cfg["target_var"], precision)
    z = coordinates[: coordinates.shape[0] - horizon]
    actual = target[horizon:]
    prediction = predict_quantiles(z, predictor_cfg, precision)
    predictions = prediction["predictions"]
    median_index = predictor_cfg["quantiles"].index(0.5) if 0.5 in predictor_cfg["quantiles"] else len(predictor_cfg["quantiles"]) // 2
    residuals = precision.sub(actual, predictions[:, median_index])

    return {
        "window": reduction_result["window"],
        "v_idx": reduction_result["v_idx"],
        "coordinates": z,
        "actual": actual,
        "residuals": residuals,
        **prediction,
        "median_index": median_index,
        "model": {
            "coefficients": predictor_cfg["coefficients"],
            "intercepts": predictor_cfg["intercepts"],
            "quantiles": predictor_cfg["quantiles"],
            "target_var": predictor_cfg["target_var"],
        },
    }


def compare_quantile_vectors(
    left: np.ndarray,
    right: np.ndarray,
    strategy: str = "strict",
    tolerance: float = 0.0,
    near_band: float = 0.0,
) -> dict[str, Any]:
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    if a.shape != b.shape:
        raise quantile_order("quantile comparison shapes differ", left_shape=list(a.shape), right_shape=list(b.shape))
    difference = np.abs(a - b)
    if strategy == "strict":
        passed = bool(np.array_equal(a, b))
        boundary = 0.0
    elif strategy == "tolerance":
        if tolerance < 0:
            from .errors import bad_input

            raise bad_input("tolerance must be non-negative", tolerance=tolerance)
        passed = bool(np.all(difference <= tolerance))
        boundary = float(tolerance)
    else:
        from .errors import bad_input

        raise bad_input("unknown quantile comparison strategy", strategy=strategy)

    flat_diff = difference.reshape(-1)
    near_indices = np.where(np.abs(flat_diff - boundary) <= max(near_band, 0.0))[0]
    near_samples = [
        {"flat_index": int(i), "absolute_difference": float(flat_diff[i]), "boundary": boundary}
        for i in near_indices[:50]
    ]
    return {
        "strategy": strategy,
        "tolerance": float(tolerance),
        "near_band": float(near_band),
        "passed": passed,
        "max_abs_difference": float(np.max(difference)) if difference.size else 0.0,
        "near_boundary_samples": near_samples,
    }


def compare_observations_to_quantiles(
    actual: np.ndarray,
    predictions: np.ndarray,
    levels: list[float],
    tolerance: float,
    near_band: float,
) -> dict[str, Any]:
    """Compare observations with predicted quantile boundaries in two modes."""
    y = np.asarray(actual, dtype=np.float64)
    qhat = np.asarray(predictions, dtype=np.float64)
    strict_results: dict[str, Any] = {}
    tolerance_results: dict[str, Any] = {}
    near_samples: list[dict[str, Any]] = []

    for col, level in enumerate(levels):
        signed_gap = y - qhat[:, col]
        strict_inside = signed_gap <= 0.0
        tolerance_inside = signed_gap <= max(float(tolerance), 0.0)
        near_boundary = np.abs(signed_gap) <= max(float(near_band), 0.0)
        strict_results[str(level)] = {
            "inside_count": int(strict_inside.sum()),
            "outside_count": int((~strict_inside).sum()),
            "inside_rate": float(np.mean(strict_inside)),
        }
        tolerance_results[str(level)] = {
            "tolerance": float(tolerance),
            "inside_count": int(tolerance_inside.sum()),
            "outside_count": int((~tolerance_inside).sum()),
            "inside_rate": float(np.mean(tolerance_inside)),
        }
        for row in np.where(near_boundary)[0][:20]:
            near_samples.append(
                {
                    "grid_row": int(row),
                    "quantile": float(level),
                    "signed_gap": float(signed_gap[row]),
                    "abs_gap": float(abs(signed_gap[row])),
                    "near_band": float(near_band),
                }
            )

    return {
        "strict": strict_results,
        "tolerance_band": tolerance_results,
        "near_boundary_samples": near_samples[:100],
    }


def _drift_statistics(
    residuals: np.ndarray,
    preprocess_result: dict[str, Any],
    config: dict[str, Any],
    precision: Precision,
    previous_drift: dict[str, Any] | None,
) -> dict[str, Any]:
    drift_cfg = config.get("drift", {})
    if residuals.size < int(drift_cfg.get("min_residuals", 1)):
        raise drift_missing(
            "residual series is too short for drift statistics",
            residual_count=int(residuals.size),
            min_residuals=int(drift_cfg.get("min_residuals", 1)),
        )
    residual_mean = precision.mean(residuals)
    residual_var = precision.var(residuals)
    residual_std = precision.sqrt(residual_var)
    decay = float(drift_cfg.get("residual_decay", 0.85))
    previous_ewm = None if not previous_drift else previous_drift.get("residual_ewm_mean")
    if previous_ewm is None:
        residual_ewm = residual_mean
    else:
        residual_ewm = precision.add(
            precision.mul(previous_ewm, decay),
            precision.mul(residual_mean, 1.0 - decay),
        )

    means = np.asarray([preprocess_result["stats"][name]["mean"] for name in preprocess_result["variables"]])
    if previous_drift and "feature_mean_vector" in previous_drift:
        previous_means = np.asarray(previous_drift["feature_mean_vector"], dtype=np.float64)
        if previous_means.shape != means.shape:
            raise drift_missing("previous feature mean vector shape mismatch")
        feature_shift = precision.sqrt(precision.mean(precision.square(precision.sub(means, previous_means))))
    else:
        feature_shift = precision.q(0.0)

    denominator = precision.add(residual_std, 1e-8)
    standardized_residual_drift = precision.div(np.abs(residual_ewm), denominator)
    score = precision.add(standardized_residual_drift, feature_shift)
    required = ["residual_mean", "residual_var", "residual_ewm_mean", "feature_shift", "score"]
    values = {
        "residual_mean": float(residual_mean),
        "residual_var": float(residual_var),
        "residual_std": float(residual_std),
        "residual_ewm_mean": float(residual_ewm),
        "feature_shift": float(feature_shift),
        "score": float(score),
        "feature_mean_vector": [float(x) for x in means],
    }
    missing = [key for key in required if key not in values or not np.isfinite(values[key])]
    if missing:
        raise drift_missing("computed drift statistics are missing", missing=missing)
    return values


def build_error_spectrum(
    prediction_result: dict[str, Any],
    reduction_result: dict[str, Any],
    preprocess_result: dict[str, Any],
    config: dict[str, Any],
    precision: Precision,
    previous_drift: dict[str, Any] | None = None,
) -> dict[str, Any]:
    predictions = np.asarray(prediction_result["predictions"], dtype=np.float64)
    actual = np.asarray(prediction_result["actual"], dtype=np.float64)
    residuals = np.asarray(prediction_result["residuals"], dtype=np.float64)
    levels = prediction_result["quantiles"]
    gating_cfg = config.get("gating", {})
    threshold = float(gating_cfg.get("error_threshold", 2.0))
    tolerance = float(gating_cfg.get("tolerance", 0.0))
    strategy = gating_cfg.get("strategy", "tolerance")
    near_band = float(gating_cfg.get("near_band", 0.0))

    quantile_metrics: dict[str, Any] = {}
    for index, level in enumerate(levels):
        errors = precision.sub(actual, predictions[:, index])
        pinball = np.where(errors >= 0, level * errors, (level - 1.0) * errors)
        quantile_metrics[str(level)] = {
            "mean_error": float(precision.mean(errors)),
            "mae": float(precision.mean(np.abs(errors))),
            "rmse": float(precision.sqrt(precision.mean(precision.square(errors)))),
            "pinball_loss": float(precision.mean(pinball)),
            "coverage_below_quantile": float(precision.mean((actual <= predictions[:, index]).astype(np.float64))),
        }

    boundary = threshold if strategy == "strict" else threshold + tolerance
    gated_in = np.abs(residuals) <= boundary
    near_threshold = np.abs(np.abs(residuals) - threshold) <= near_band
    near_tolerance_boundary = np.abs(np.abs(residuals) - boundary) <= near_band
    near_samples = []
    for row in np.where(near_threshold | near_tolerance_boundary)[0]:
        near_samples.append(
            {
                "grid_row": int(row),
                "abs_residual": float(abs(residuals[row])),
                "threshold": threshold,
                "effective_boundary": boundary,
                "near_nominal": bool(near_threshold[row]),
                "near_effective": bool(near_tolerance_boundary[row]),
            }
        )

    drift = _drift_statistics(residuals, preprocess_result, config, precision, previous_drift)
    drift["threshold"] = float(config.get("drift", {}).get("score_threshold", threshold))
    drift["gate"] = "open" if drift["score"] > drift["threshold"] else "closed"
    quantile_comparison = compare_observations_to_quantiles(
        actual,
        predictions,
        levels,
        float(gating_cfg.get("quantile_tolerance", tolerance)),
        float(gating_cfg.get("quantile_near_band", near_band)),
    )

    spectrum = {
        "window": prediction_result["window"],
        "v_idx": prediction_result["v_idx"],
        "residuals": residuals,
        "actual": actual,
        "quantile_metrics": quantile_metrics,
        "gating": {
            "strategy": strategy,
            "error_threshold": threshold,
            "tolerance": tolerance,
            "effective_boundary": boundary,
            "near_band": near_band,
            "accepted": int(gated_in.sum()),
            "rejected": int((~gated_in).sum()),
            "accept_rate": float(precision.mean(gated_in.astype(np.float64))),
            "near_boundary_samples": near_samples[:100],
        },
        "quantile_comparison": quantile_comparison,
        "drift": drift,
        "precision": precision.config_view(),
    }
    missing = [key for key in ("residuals", "quantile_metrics", "gating", "drift") if key not in spectrum]
    if missing:
        raise drift_missing("error spectrum is missing required sections", missing=missing)
    return spectrum
