from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any

from .fixedpoint import high_scale_value, round_half_up
from .thresholds import ThresholdCandidate, threshold_for_scope


def _threshold_verdict(value: Decimal, thresholds: dict[str, ThresholdCandidate], direction: str) -> str:
    warn = thresholds.get("warn")
    fail = thresholds.get("fail")
    if direction == "higher_is_worse":
        if fail is not None and value >= fail.value:
            return "fail"
        if warn is not None and value >= warn.value:
            return "warn"
        return "ok"

    if fail is not None and value <= fail.value:
        return "fail"
    if warn is not None and value <= warn.value:
        return "warn"
    return "ok"


def _rounded_verdict(raw: Fraction, scale: int, thresholds: dict[str, ThresholdCandidate], direction: str) -> str:
    rounded, _, _, _ = round_half_up(raw * scale)
    value = Decimal(rounded) / Decimal(scale)
    return _threshold_verdict(value, thresholds, direction)


def evaluate_metric(
    *,
    metric: str,
    definition: dict[str, Any],
    raw_value: Fraction,
    truth_value: Decimal,
    fixed_value: int,
    error_bound: Decimal,
    threshold_view: dict[tuple[str, str, str], ThresholdCandidate],
    scope_id: str,
    service_name: str | None,
    metric_version: str,
    threshold_version: str,
) -> dict[str, Any] | None:
    thresholds = threshold_for_scope(
        threshold_view,
        scope_id=scope_id,
        service_name=service_name,
        metric=metric,
    )
    if thresholds is None:
        return None

    direction = str(definition.get("direction", "higher_is_worse"))
    scale = int(definition["scale"])
    verdict = _threshold_verdict(truth_value, thresholds, direction)
    current_rounded_verdict = _rounded_verdict(raw_value, scale, thresholds, direction)

    probe_multiplier = int(definition.get("probe_scale_multiplier", 1000))
    probe_scale = scale * probe_multiplier
    high_verdict = _rounded_verdict(raw_value, probe_scale, thresholds, direction)

    threshold_distances = []
    for candidate in thresholds.values():
        threshold_distances.append(abs(truth_value - candidate.value))
    nearest_distance = min(threshold_distances) if threshold_distances else None
    edge_sensitive = nearest_distance is not None and nearest_distance <= error_bound
    higher_scale_flip = current_rounded_verdict != high_verdict
    precision_sensitive = edge_sensitive or higher_scale_flip

    safe_verdict = verdict
    if edge_sensitive and higher_scale_flip:
        safe_verdict = "precision_review"

    return {
        "metric": metric,
        "metric_version": metric_version,
        "threshold_version": threshold_version,
        "scope_id": scope_id,
        "verdict": verdict,
        "safe_verdict": safe_verdict,
        "authoritative_path": "decimal_high_precision",
        "fixed_value": fixed_value,
        "truth_value": format(truth_value, "f"),
        "error_bound": format(error_bound, "f"),
        "nearest_threshold_distance": format(nearest_distance, "f") if nearest_distance is not None else None,
        "edge_sensitive": edge_sensitive,
        "precision_sensitive": precision_sensitive,
        "higher_scale_flip": higher_scale_flip,
        "current_rounded_verdict": current_rounded_verdict,
        "higher_scale_verdict": high_verdict,
        "probe_scale": probe_scale,
        "thresholds": {
            level: candidate.to_dict() for level, candidate in sorted(thresholds.items())
        },
    }


def compare_pair(
    *,
    metric: str,
    definition: dict[str, Any],
    scope_a: str,
    scope_b: str,
    raw_a: Fraction,
    raw_b: Fraction,
    truth_a: Decimal,
    truth_b: Decimal,
    tolerance: Decimal,
    relation_type: str,
) -> dict[str, Any]:
    scale = int(definition["scale"])
    direction = str(definition.get("direction", "higher_is_worse"))
    diff = abs(truth_a - truth_b)
    sensitive = diff < tolerance

    def ranking_from_values(value_a: Decimal | int, value_b: Decimal | int) -> str:
        if value_a == value_b:
            return "tie"
        a_better = value_a < value_b if direction == "higher_is_worse" else value_a > value_b
        if a_better:
            return f"{scope_a}_preferred"
        return f"{scope_b}_preferred"

    current_a = high_scale_value(raw_a, scale)
    current_b = high_scale_value(raw_b, scale)
    current_ranking = ranking_from_values(current_a, current_b)

    probe_multiplier = int(definition.get("probe_scale_multiplier", 1000))
    probe_scale = scale * probe_multiplier
    high_a = high_scale_value(raw_a, probe_scale)
    high_b = high_scale_value(raw_b, probe_scale)
    high_ranking = ranking_from_values(high_a, high_b)
    truth_ranking = ranking_from_values(truth_a, truth_b)

    higher_scale_flip = current_ranking != high_ranking
    safe_ranking = truth_ranking
    if sensitive:
        safe_ranking = "precision_sensitive_indeterminate_within_tolerance"

    return {
        "metric": metric,
        "scope_a": scope_a,
        "scope_b": scope_b,
        "relation_type": relation_type,
        "direction": direction,
        "value_a_truth": format(truth_a, "f"),
        "value_b_truth": format(truth_b, "f"),
        "absolute_difference": format(diff, "f"),
        "tolerance": format(tolerance, "f"),
        "sensitive": sensitive,
        "conclusion_depends_on_difference": True,
        "current_scale": scale,
        "current_fixed_a": current_a,
        "current_fixed_b": current_b,
        "current_naive_ranking": current_ranking,
        "probe_scale": probe_scale,
        "high_fixed_a": high_a,
        "high_fixed_b": high_b,
        "higher_scale_ranking": high_ranking,
        "higher_scale_flip": higher_scale_flip,
        "truth_ranking": truth_ranking,
        "safe_ranking": safe_ranking,
        "rounding_cannot_flip_safe_conclusion": True,
    }
