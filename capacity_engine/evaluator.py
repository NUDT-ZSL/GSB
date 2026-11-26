from __future__ import annotations

from itertools import combinations
from decimal import Decimal
from fractions import Fraction
from typing import Any

from .errors import CapacityError, ErrorCode
from .hashing import sha256_object
from .precision import (
    Ledger,
    convert_observation_metric,
    compute_decimal_metric,
    fraction_to_decimal,
    decimal_to_fraction,
)
from .thresholds import threshold_fixed_value
from .validation import METRIC_NAMES
from .windows import all_observations, build_window_plan


def evaluate_metric_values(
    input_data: Any,
    metric_definitions: dict[str, Any],
    ledger: Ledger | None = None,
    *,
    only_keys: set[tuple[int, str, str]] | None = None,
    prior_values: list[dict[str, Any]] | None = None,
    prior_ledger_entries: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], Ledger]:
    plan = build_window_plan(input_data.traces, metric_definitions)
    observations = all_observations(input_data, plan)
    if ledger is None:
        ledger = Ledger()
        if prior_ledger_entries:
            ledger.entries = [dict(item) for item in prior_ledger_entries]
            ledger.seed_seq(max((int(item["seq"]) for item in ledger.entries), default=0))

    prior_by_result_key = {
        (item["window_no"], item["instance_id"], item["metric"]): item
        for item in (prior_values or [])
    }
    results: list[dict[str, Any]] = []
    for observation in observations:
        for metric_name in METRIC_NAMES:
            key = (observation["window_no"], observation["instance_id"], metric_name)
            obs_scope = (observation["window_no"], observation["instance_id"])
            if only_keys is not None and key not in only_keys:
                if key in prior_by_result_key:
                    results.append(prior_by_result_key[key])
                continue
            if only_keys is None and key in prior_by_result_key:
                results.append(prior_by_result_key[key])
                continue
            spec = metric_definitions["metrics"][metric_name]
            result = convert_observation_metric(observation, metric_name, spec, ledger)
            results.append(result)
    results.sort(key=lambda item: (item["window_no"], item["instance_id"], item["metric"]))
    return results, ledger


def assert_precision_budget(values: list[dict[str, Any]]) -> None:
    violations: list[dict[str, Any]] = []
    for value in values:
        deviation = Decimal(value["absolute_deviation"])
        budget = Decimal(value["error_upper_bound_physical"])
        if deviation > budget + Decimal("1e-48"):
            violations.append(
                {
                    "obs_key": value["obs_key"],
                    "metric": value["metric"],
                    "absolute_deviation": value["absolute_deviation"],
                    "error_upper_bound_physical": value["error_upper_bound_physical"],
                    "ledger_seqs": value["ledger_seqs"],
                }
            )
    if violations:
        raise CapacityError(
            ErrorCode.PRECISION_BUDGET_EXCEEDED,
            "fixed-point deviation exceeds cumulative error upper-bound budget",
            {"violations": violations},
        )


def _orientation(metric: str, threshold: dict[str, Any] | None) -> str:
    if threshold and threshold.get("operator") in {"max", "lt", "lte"}:
        return "upper"
    if threshold and threshold.get("operator") in {"min", "gt", "gte"}:
        return "lower"
    return "lower" if metric == "throughput_attainment" else "upper"


def evaluate_conclusion(
    value: dict[str, Any],
    threshold_view: dict[str, dict[str, Any]],
    metric_definitions: dict[str, Any],
) -> dict[str, Any]:
    metric = value["metric"]
    level = value["service_level"]
    key = f"{level}.{metric}"
    threshold = threshold_view.get(key)
    spec = metric_definitions["metrics"][metric]
    candidates: list[dict[str, Any]] = []
    base = {
        "obs_key": value["obs_key"],
        "window_no": value["window_no"],
        "service_name": value["service_name"],
        "instance_id": value["instance_id"],
        "service_level": level,
        "metric": metric,
        "threshold_key": key,
    }
    if not threshold or not threshold.get("active", False):
        conclusion = {
            **base,
            "capacity_ok": None,
            "capacity_state": "threshold_tombstoned",
            "inflection_triggered": False,
            "threshold_evidence": None if not threshold else {
                "threshold_key": key,
                "active": False,
                "tombstoned": True,
                "source_version": threshold.get("source_version"),
                "merge_reason": threshold.get("merge_reason"),
            },
            "candidate_evaluations": [
                {"candidate": "threshold_available", "selected": False, "rejection_reason": "threshold is tombstoned or missing"},
                {"candidate": "indeterminate_tombstoned", "selected": True, "rejection_reason": None},
            ],
            "conclusion_digest": sha256_object({**base, "state": "threshold_tombstoned"}),
        }
        return conclusion

    orientation = _orientation(metric, threshold)
    threshold_decimal = Decimal(str(threshold["value"]))
    threshold_fixed = threshold_fixed_value(threshold, value["scale"], value["rounding"])
    fixed_value = int(value["fixed_value"])
    hp_value = Decimal(value["decimal_value"])
    if orientation == "upper":
        fixed_fail = fixed_value >= threshold_fixed["fixed_value"]
        hp_fail = hp_value >= threshold_decimal
        distance = threshold_decimal - hp_value
        fail_candidate = "at_or_above_capacity_threshold"
        safe_candidate = "below_capacity_threshold"
    else:
        fixed_fail = fixed_value <= threshold_fixed["fixed_value"]
        hp_fail = hp_value <= threshold_decimal
        distance = hp_value - threshold_decimal
        fail_candidate = "at_or_below_minimum_threshold"
        safe_candidate = "above_minimum_threshold"

    error_bound = Decimal(value["error_upper_bound_physical"])
    margin_tolerance = Decimal(str(spec.get("threshold_margin_tolerance", "0")))
    near_band = max(error_bound, margin_tolerance)
    near = (not hp_fail) and abs(distance) <= near_band
    if hp_fail:
        state = "capacity_threshold_crossed"
        selected = fail_candidate
        rejected = safe_candidate
        reject_reason = f"high-precision comparison crosses threshold by {abs(distance)}"
    elif near:
        state = "capacity_inflection_band"
        selected = "near_threshold_inflection"
        rejected = safe_candidate
        reject_reason = f"distance {distance} is within inflection band {near_band}"
    else:
        state = "stable_with_margin"
        selected = safe_candidate
        rejected = fail_candidate
        reject_reason = f"distance {distance} exceeds inflection band {near_band}"

    candidates = [
        {"candidate": selected, "selected": True, "rejection_reason": None},
        {"candidate": rejected, "selected": False, "rejection_reason": reject_reason},
    ]
    if near or hp_fail:
        candidates.append(
            {
                "candidate": "far_from_threshold",
                "selected": False,
                "rejection_reason": f"absolute threshold distance {abs(distance)} does not support stable classification",
            }
        )
    conclusion = {
        **base,
        "orientation": orientation,
        "capacity_ok": not hp_fail,
        "fixed_capacity_ok": not fixed_fail,
        "conclusion_flipped_between_fixed_and_decimal": fixed_fail != hp_fail,
        "capacity_state": state,
        "selected_candidate": selected,
        "rejected_candidate": rejected,
        "candidate_evaluations": candidates,
        "threshold_distance": format(distance, "f"),
        "inflection_band": format(near_band, "f"),
        "inflection_triggered": hp_fail or near,
        "threshold_evidence": {
            "threshold_key": key,
            "threshold_value": format(threshold_decimal, "f"),
            "threshold_fixed_value": threshold_fixed["fixed_value"],
            "scale": value["scale"],
            "operator": threshold.get("operator"),
            "orientation": orientation,
            "source_version": threshold.get("source_version"),
            "source_format": threshold.get("source_format"),
            "logical_clock_ms": threshold.get("logical_clock_ms"),
            "generation": threshold.get("generation"),
            "merge_reason": threshold.get("merge_reason"),
            "original_time_representation": threshold.get("original_time_representation"),
        },
        "fixed_comparison": {
            "metric_fixed_value": fixed_value,
            "threshold_fixed_value": threshold_fixed["fixed_value"],
            "fixed_capacity_ok": not fixed_fail,
        },
        "decimal_comparison": {
            "metric_decimal_value": value["decimal_value"],
            "threshold_decimal_value": format(threshold_decimal, "f"),
            "decimal_capacity_ok": not hp_fail,
        },
        "ledger_seqs": value["ledger_seqs"],
        "error_upper_bound_physical": value["error_upper_bound_physical"],
    }
    conclusion["conclusion_digest"] = sha256_object(
        {k: v for k, v in conclusion.items() if k != "conclusion_digest"}
    )
    return conclusion


def build_conclusions(
    values: list[dict[str, Any]],
    threshold_view: dict[str, dict[str, Any]],
    metric_definitions: dict[str, Any],
) -> list[dict[str, Any]]:
    conclusions = [evaluate_conclusion(value, threshold_view, metric_definitions) for value in values]
    conclusions.sort(key=lambda item: (item["window_no"], item["instance_id"], item["metric"]))
    return conclusions


def _order(left: Decimal, right: Decimal) -> str:
    if left == right:
        return "equal"
    return "a_gt_b" if left > right else "b_gt_a"


def _critical(order: str, orientation: str) -> str:
    if order == "equal":
        return "tie"
    if orientation == "upper":
        return "a" if order == "a_gt_b" else "b"
    return "b" if order == "a_gt_b" else "a"


def _higher_scale_order(value_a: dict[str, Any], value_b: dict[str, Any], multiplier: int) -> tuple[str, int, int]:
    high_scale = int(value_a["scale"]) * int(multiplier)
    raw_a = decimal_to_fraction(value_a["raw_value_decimal"])
    raw_b = decimal_to_fraction(value_b["raw_value_decimal"])

    def rounded(raw: Fraction) -> int:
        scaled = raw * high_scale
        q, rem = divmod(scaled.numerator, scaled.denominator)
        if rem and 2 * rem >= scaled.denominator:
            q += 1
        return q

    high_a = rounded(raw_a)
    high_b = rounded(raw_b)
    order = "equal" if high_a == high_b else ("a_gt_b" if high_a > high_b else "b_gt_a")
    return order, high_a, high_b


def build_sensitivity(
    values: list[dict[str, Any]],
    conclusions: list[dict[str, Any]],
    metric_definitions: dict[str, Any],
) -> list[dict[str, Any]]:
    value_by_key = {(item["obs_key"], item["metric"]): item for item in values}
    conclusion_by_key = {(item["obs_key"], item["metric"]): item for item in conclusions}
    assessments: list[dict[str, Any]] = []

    pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    by_window_metric: dict[tuple[int, str], list[dict[str, Any]]] = {}
    by_instance_metric: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for value in values:
        by_window_metric.setdefault((value["window_no"], value["metric"]), []).append(value)
        by_instance_metric.setdefault((value["instance_id"], value["metric"]), []).append(value)

    for (window_no, metric), items in sorted(by_window_metric.items()):
        for left, right in combinations(sorted(items, key=lambda x: x["instance_id"]), 2):
            pairs.append(("cross_service_same_window", left, right))
    for (instance_id, metric), items in sorted(by_instance_metric.items()):
        for left, right in combinations(sorted(items, key=lambda x: x["window_no"]), 2):
            pairs.append(("cross_window_same_instance", left, right))

    for pair_kind, value_a, value_b in sorted(
        pairs, key=lambda item: (item[1]["metric"], item[0], item[1]["obs_key"], item[2]["obs_key"])
    ):
        metric = value_a["metric"]
        spec = metric_definitions["metrics"][metric]
        orientation = _orientation(metric, None)
        decimal_a = Decimal(value_a["decimal_value"])
        decimal_b = Decimal(value_b["decimal_value"])
        fixed_a = int(value_a["fixed_value"])
        fixed_b = int(value_b["fixed_value"])
        hp_order = _order(decimal_a, decimal_b)
        fixed_order = "equal" if fixed_a == fixed_b else ("a_gt_b" if fixed_a > fixed_b else "b_gt_a")
        multiplier = int(spec.get("higher_scale_multiplier", 100))
        high_order, high_a, high_b = _higher_scale_order(value_a, value_b, multiplier)
        hp_critical = _critical(hp_order, orientation)
        fixed_critical = _critical(fixed_order, orientation)
        high_critical = _critical(high_order, orientation)
        delta = abs(decimal_a - decimal_b)
        tolerance = Decimal(str(spec.get("pair_tolerance", "0")))
        bound_a = Decimal(value_a["error_upper_bound_physical"])
        bound_b = Decimal(value_b["error_upper_bound_physical"])
        combined_bound = bound_a + bound_b
        conclusion_a = conclusion_by_key[(value_a["obs_key"], metric)]
        conclusion_b = conclusion_by_key[(value_b["obs_key"], metric)]
        statuses_differ = conclusion_a.get("capacity_state") != conclusion_b.get("capacity_state")
        order_mismatch = fixed_order != hp_order
        higher_scale_flip = fixed_order != high_order or fixed_critical != high_critical
        depends_on_difference = (
            order_mismatch
            or fixed_critical != hp_critical
            or higher_scale_flip
            or (statuses_differ and delta < tolerance)
        )
        sensitive = delta < tolerance and depends_on_difference
        ledger_seqs = sorted(set(value_a["ledger_seqs"]) | set(value_b["ledger_seqs"]))
        key = sha256_object(
            {
                "pair_kind": pair_kind,
                "metric": metric,
                "a": value_a["obs_key"],
                "b": value_b["obs_key"],
            }
        )[:16]
        rejected = []
        selected_candidate = f"critical_endpoint_{hp_critical}"
        for candidate in ("a", "b", "tie"):
            selected = hp_critical == candidate
            rejected.append(
                {
                    "candidate": f"critical_endpoint_{candidate}",
                    "selected": selected,
                    "rejection_reason": None
                    if selected
                    else f"high-precision ordering {hp_order} selects {hp_critical}",
                }
            )
        record = {
            "sensitivity_key": key,
            "pair_kind": pair_kind,
            "metric": metric,
            "orientation": orientation,
            "obs_a": value_a["obs_key"],
            "obs_b": value_b["obs_key"],
            "window_numbers": sorted({value_a["window_no"], value_b["window_no"]}),
            "services": sorted({value_a["service_name"], value_b["service_name"]}),
            "instances": sorted({value_a["instance_id"], value_b["instance_id"]}),
            "decimal_value_a": value_a["decimal_value"],
            "decimal_value_b": value_b["decimal_value"],
            "difference": format(delta, "f"),
            "tolerance": format(tolerance, "f"),
            "within_tolerance": delta < tolerance,
            "fixed_order": fixed_order,
            "high_precision_order": hp_order,
            "higher_scale_order": high_order,
            "higher_scale": int(value_a["scale"]) * multiplier,
            "higher_scale_values": [high_a, high_b],
            "fixed_critical_endpoint": fixed_critical,
            "high_precision_critical_endpoint": hp_critical,
            "higher_scale_critical_endpoint": high_critical,
            "conclusion_depends_on_difference": depends_on_difference,
            "sensitive": sensitive,
            "would_flip_at_higher_scale": bool(higher_scale_flip and sensitive),
            "order_mismatch_fixed_vs_decimal": order_mismatch,
            "threshold_statuses_differ": statuses_differ,
            "combined_error_upper_bound_physical": format(combined_bound, "f"),
            "error_source_chain": [
                {
                    "obs_key": value_a["obs_key"],
                    "ledger_seqs": value_a["ledger_seqs"],
                    "error_upper_bound_physical": value_a["error_upper_bound_physical"],
                },
                {
                    "obs_key": value_b["obs_key"],
                    "ledger_seqs": value_b["ledger_seqs"],
                    "error_upper_bound_physical": value_b["error_upper_bound_physical"],
                },
            ],
            "ledger_seqs": ledger_seqs,
            "candidate_decisions": rejected,
            "threshold_evidence": [
                conclusion_a.get("threshold_evidence"),
                conclusion_b.get("threshold_evidence"),
            ],
        }
        record["record_digest"] = sha256_object({k: v for k, v in record.items() if k != "record_digest"})
        assessments.append(record)
    assessments.sort(key=lambda item: item["sensitivity_key"])
    return assessments


def error_chain_report(values: list[dict[str, Any]], ledger: Ledger) -> list[dict[str, Any]]:
    entries_by_obs_metric: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in ledger.entries:
        if entry["window_no"] < 0:
            continue
        obs_key = f"W{entry['window_no']}:{entry['instance_id']}"
        entries_by_obs_metric.setdefault((obs_key, entry["metric"]), []).append(entry)
    chains = []
    for value in values:
        chains.append(
            {
                "window_no": value["window_no"],
                "obs_key": value["obs_key"],
                "service_name": value["service_name"],
                "instance_id": value["instance_id"],
                "metric": value["metric"],
                "request_seq_interval": [value["request_seq_min"], value["request_seq_max"]],
                "request_count": value["request_count"],
                "request_set_digest": sha256_object(value["request_seqs"]),
                "scale": value["scale"],
                "rounding": value["rounding"],
                "error_upper_bound_physical": value["error_upper_bound_physical"],
                "error_upper_bound_fixed_units": value["error_upper_bound_fixed_units"],
                "ledger_seqs": value["ledger_seqs"],
                "entries": entries_by_obs_metric.get((value["obs_key"], value["metric"]), []),
            }
        )
    chains.sort(key=lambda item: (item["window_no"], item["instance_id"], item["metric"]))
    return chains


def cumulative_window_error_report(chains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows: dict[int, dict[str, Any]] = {}
    for chain in chains:
        window_no = chain["window_no"]
        bucket = windows.setdefault(
            window_no,
            {
                "window_no": window_no,
                "chain_count": 0,
                "ledger_entry_count": 0,
                "request_intervals": [],
                "cumulative_physical_bound_by_metric": {},
                "cumulative_fixed_unit_bound_by_obs_metric": {},
            },
        )
        bucket["chain_count"] += 1
        bucket["ledger_entry_count"] += len(chain["entries"])
        bucket["request_intervals"].append(
            {
                "obs_key": chain["obs_key"],
                "metric": chain["metric"],
                "interval": chain["request_seq_interval"],
                "request_count": chain["request_count"],
            }
        )
        metric = chain["metric"]
        bound = Decimal(chain["error_upper_bound_physical"])
        bucket["cumulative_physical_bound_by_metric"][metric] = format(
            Decimal(bucket["cumulative_physical_bound_by_metric"].get(metric, "0")) + bound,
            "f",
        )
        obs_metric = f"{chain['obs_key']}:{metric}"
        existing = Fraction(
            bucket["cumulative_fixed_unit_bound_by_obs_metric"].get(obs_metric, "0")
        )
        bucket["cumulative_fixed_unit_bound_by_obs_metric"][obs_metric] = str(
            existing + Fraction(chain["error_upper_bound_fixed_units"])
        )
    result = [windows[key] for key in sorted(windows)]
    for bucket in result:
        bucket["digest"] = sha256_object(
            {k: v for k, v in bucket.items() if k != "digest"}
        )
    return result


def decimal_full_recompute(
    input_data: Any, metric_definitions: dict[str, Any]
) -> dict[str, Any]:
    plan = build_window_plan(input_data.traces, metric_definitions)
    observations = all_observations(input_data, plan)
    rows: list[dict[str, Any]] = []
    for observation in observations:
        for metric_name in METRIC_NAMES:
            value = compute_decimal_metric(
                observation, metric_name, metric_definitions["metrics"][metric_name]
            )
            rows.append(
                {
                    "obs_key": f"W{observation['window_no']}:{observation['instance_id']}",
                    "window_no": observation["window_no"],
                    "instance_id": observation["instance_id"],
                    "metric": metric_name,
                    "decimal_value": format(value, "f"),
                }
            )
    rows.sort(key=lambda item: (item["window_no"], item["instance_id"], item["metric"]))
    return {
        "decimal_context_precision": 60,
        "row_count": len(rows),
        "rows": rows,
        "full_recompute_digest": sha256_object(rows),
    }


def build_evaluation_bundle(
    input_data: Any,
    metric_definitions: dict[str, Any],
    threshold_view: dict[str, dict[str, Any]],
    values: list[dict[str, Any]],
    ledger: Ledger,
) -> dict[str, Any]:
    assert_precision_budget(values)
    conclusions = build_conclusions(values, threshold_view, metric_definitions)
    sensitivity = build_sensitivity(values, conclusions, metric_definitions)
    chains = error_chain_report(values, ledger)
    window_errors = cumulative_window_error_report(chains)
    decimal_truth = decimal_full_recompute(input_data, metric_definitions)
    truth_by_key = {(row["obs_key"], row["metric"]): row["decimal_value"] for row in decimal_truth["rows"]}
    for value in values:
        expected = truth_by_key[(value["obs_key"], value["metric"])]
        if abs(Decimal(expected) - Decimal(value["decimal_value"])) > Decimal("1e-48"):
            raise CapacityError(
                ErrorCode.INTERNAL,
                "Decimal full recompute does not match comparison truth value",
                {"obs_key": value["obs_key"], "metric": value["metric"]},
            )
    return {
        "values": values,
        "ledger_entries": ledger.entries,
        "conclusions": conclusions,
        "sensitivity_assessments": sensitivity,
        "sensitive_records": [item for item in sensitivity if item["sensitive"]],
        "inflection_records": [item for item in conclusions if item["inflection_triggered"]],
        "error_chains": chains,
        "window_error_summaries": window_errors,
        "decimal_full_recompute": decimal_truth,
    }


def full_evaluation(
    input_data: Any,
    metric_definitions: dict[str, Any],
    threshold_view: dict[str, dict[str, Any]],
    *,
    ledger: Ledger | None = None,
) -> dict[str, Any]:
    values, ledger = evaluate_metric_values(input_data, metric_definitions, ledger=ledger)
    return build_evaluation_bundle(
        input_data, metric_definitions, threshold_view, values, ledger
    )
