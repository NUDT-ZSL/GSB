"""Feature drift measures, decisions, severity, and deterministic ranking."""

from __future__ import annotations

import math
from typing import Any, Iterable

from .canonical import sha256_obj, stable_float
from .models import CATEGORY, MEAN_SHIFT, NUMERIC, QUANTILE_DISTANCE, FeatureSpec, MonitorConfig


SEVERITY_NONE = "NONE"
SEVERITY_LOW = "LOW"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_HIGH = "HIGH"
SEVERITY_DISABLED = "DISABLED"
SEVERITY_INSUFFICIENT = "INSUFFICIENT_REFERENCE"
SEVERITY_ORDER = {
    SEVERITY_NONE: 0,
    SEVERITY_LOW: 1,
    SEVERITY_MEDIUM: 2,
    SEVERITY_HIGH: 3,
}


def severity_for(drift: float, threshold: float) -> str:
    if drift < threshold:
        return SEVERITY_NONE
    ratio = stable_float(drift / threshold)
    if ratio < 1.5:
        return SEVERITY_LOW
    if ratio < 2.5:
        return SEVERITY_MEDIUM
    return SEVERITY_HIGH


def _welch_diagnostic(reference: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    n_ref = reference["valid_count"]
    n_cur = current["valid_count"]
    if not n_ref or not n_cur:
        return {"welch_t": None, "p_value": None}
    var_ref = float(reference["variance"])
    var_cur = float(current["variance"])
    denominator = math.sqrt(var_ref / n_ref + var_cur / n_cur)
    if denominator <= 0:
        return {"welch_t": None, "p_value": None}
    t_statistic = (float(current["mean"]) - float(reference["mean"])) / denominator
    degrees = (var_ref / n_ref + var_cur / n_cur) ** 2
    degrees /= max(
        (var_ref / n_ref) ** 2 / max(n_ref - 1, 1)
        + (var_cur / n_cur) ** 2 / max(n_cur - 1, 1),
        1e-30,
    )
    try:
        from scipy.stats import t

        p_value = float(2.0 * t.sf(abs(t_statistic), degrees))
    except Exception:
        p_value = None
    return {
        "welch_t": stable_float(t_statistic),
        "p_value": stable_float(p_value) if p_value is not None else None,
        "degrees_of_freedom": stable_float(degrees),
    }


def evaluate_feature_stats(
    config: MonitorConfig, feature: FeatureSpec, stats_content: dict[str, Any]
) -> dict[str, Any]:
    if not feature.enabled:
        return {
            "artifact_type": "feature_test",
            "feature": feature.name,
            "kind": feature.kind,
            "target_batch": stats_content["target_batch"],
            "status": "disabled",
            "test_method": "disabled",
            "threshold": None,
            "drift": None,
            "detected": False,
            "severity": SEVERITY_DISABLED,
            "details": {"reason": "feature_disabled"},
            "stats_input_sha256": sha256_obj(stats_content),
            "feature_definition_sha256": feature.definition_sha256(),
            "stat_version": feature.stat_version,
        }

    reference = stats_content["statistics"]["reference"]
    current = stats_content["statistics"]["current"]
    if reference.get("valid_count", 0) == 0 or current.get("valid_count", 0) == 0:
        return {
            "artifact_type": "feature_test",
            "feature": feature.name,
            "kind": feature.kind,
            "target_batch": stats_content["target_batch"],
            "status": "insufficient_reference",
            "test_method": "none",
            "threshold": None,
            "drift": None,
            "detected": False,
            "severity": SEVERITY_INSUFFICIENT,
            "details": {
                "reference_valid_count": reference.get("valid_count", 0),
                "current_valid_count": current.get("valid_count", 0),
            },
            "stats_input_sha256": sha256_obj(stats_content),
            "feature_definition_sha256": feature.definition_sha256(),
            "stat_version": feature.stat_version,
        }

    if feature.kind == NUMERIC:
        threshold = stable_float(config.numeric_threshold)
        reference_std = max(float(reference["std"]), 1e-12)
        if config.numeric_test == MEAN_SHIFT:
            method = MEAN_SHIFT
            reference_value = stable_float(reference["mean"])
            current_value = stable_float(current["mean"])
            drift = stable_float(abs(current_value - reference_value) / reference_std)
            details = {
                "formula": "abs(current_mean-reference_mean)/max(reference_std,1e-12)",
                "reference_mean": reference_value,
                "current_mean": current_value,
                "scale": reference_std,
                "absolute_shift": stable_float(abs(current_value - reference_value)),
            }
            details.update(_welch_diagnostic(reference, current))
        elif config.numeric_test == QUANTILE_DISTANCE:
            method = QUANTILE_DISTANCE
            quantile_details = []
            distances = []
            for probability in config.quantiles:
                key = format(float(probability), ".12g")
                reference_value = stable_float(reference["quantiles"][key])
                current_value = stable_float(current["quantiles"][key])
                normalized = stable_float(
                    abs(current_value - reference_value) / reference_std
                )
                distances.append(normalized)
                quantile_details.append(
                    {
                        "probability": stable_float(probability),
                        "reference": reference_value,
                        "current": current_value,
                        "normalized_distance": normalized,
                    }
                )
            drift = stable_float(sum(distances) / len(distances))
            details = {
                "formula": "mean(abs(current_q-reference_q)/max(reference_std,1e-12))",
                "reference_std": reference_std,
                "quantile_distances": quantile_details,
            }
        else:
            raise AssertionError(f"unhandled numeric method: {config.numeric_test}")
    elif feature.kind == CATEGORY:
        method = "category_proportion_distance"
        threshold = stable_float(config.category_threshold)
        levels = sorted(
            set(reference["proportions"]) | set(current["proportions"])
        )
        reference_proportions = {
            level: stable_float(reference["proportions"].get(level, 0.0))
            for level in levels
        }
        current_proportions = {
            level: stable_float(current["proportions"].get(level, 0.0))
            for level in levels
        }
        per_level = {
            level: stable_float(
                abs(current_proportions[level] - reference_proportions[level])
            )
            for level in levels
        }
        drift = stable_float(0.5 * sum(per_level.values()))
        details = {
            "formula": "0.5*sum(abs(current_proportion-reference_proportion))",
            "levels": levels,
            "reference_proportions": reference_proportions,
            "current_proportions": current_proportions,
            "absolute_differences": per_level,
        }
    else:
        raise AssertionError(f"unhandled feature kind: {feature.kind}")

    detected = drift >= threshold
    severity = severity_for(drift, threshold) if detected else SEVERITY_NONE
    return {
        "artifact_type": "feature_test",
        "feature": feature.name,
        "kind": feature.kind,
        "target_batch": stats_content["target_batch"],
        "status": "ok",
        "test_method": method,
        "threshold": threshold,
        "drift": drift,
        "detected": detected,
        "severity": severity,
        "details": details,
        "stats_input_sha256": sha256_obj(stats_content),
        "feature_definition_sha256": feature.definition_sha256(),
        "stat_version": feature.stat_version,
    }


def test_key_components(
    config: MonitorConfig,
    feature: FeatureSpec,
    stats_content: dict[str, Any],
    stats_key: str,
    stats_content_sha256: str,
) -> dict[str, Any]:
    method = (
        config.numeric_test
        if feature.kind == NUMERIC
        else "category_proportion_distance"
    )
    threshold = (
        config.numeric_threshold if feature.kind == NUMERIC else config.category_threshold
    )
    return {
        "schema_version": config.schema_version,
        "artifact_type": "feature_test",
        "feature": feature.name,
        "target_batch": stats_content["target_batch"],
        "test_method": method,
        "threshold": threshold,
        "stat_version": feature.stat_version,
        "feature_definition_sha256": feature.definition_sha256(),
        "reference_window_sha256": stats_content["windows"]["reference"]["content_sha256"],
        "quantile_probabilities": list(config.quantiles),
        "upstream_product_hashes": {
            "stats": stats_content_sha256,
        },
        "stats_artifact_key": stats_key,
    }


def rank_test_results(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[float, str]:
        drift = item.get("drift")
        drift_value = float(drift) if drift is not None else -1.0
        return (-drift_value, item["feature"])

    ranked = []
    for position, item in enumerate(sorted(results, key=key), start=1):
        copy = dict(item)
        copy["rank"] = position
        ranked.append(copy)
    return ranked

