"""Deterministic sliding-window statistics, state transitions, and sensitivity."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from .errors import (
    E_CONN_REFUSED,
    E_MALFORMED_RESPONSE,
    E_OK,
    E_PROBE_IO,
    E_SAMPLE_MISMATCH,
    E_STATUS_MISMATCH,
    E_TIMEOUT,
)
from .fixed import (
    ROUNDING_MODE,
    canonical_decimal,
    ceil_decimal,
    dec,
    fixed_record,
    fixed_to_decimal,
    quantize_fixed,
)
from .model import (
    CLASSIFICATIONS,
    InstanceConfig,
    Plan,
    WorkbenchConfig,
    canonical_json,
)

ERROR_BY_CLASSIFICATION = {
    "success": E_OK,
    "status_mismatch": E_STATUS_MISMATCH,
    "timeout": E_TIMEOUT,
    "connection_refused": E_CONN_REFUSED,
    "malformed_response": E_MALFORMED_RESPONSE,
    "sample_mismatch": E_SAMPLE_MISMATCH,
    "probe_io": E_PROBE_IO,
}


def stable_check_payload(outcome: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "instance_id",
        "check_index",
        "scheduled_at_ms",
        "jitter_offset_ms",
        "classification",
        "error_code",
        "http_status",
        "expected_status",
        "body_sha256",
        "expected_body_sha256",
        "response_bytes",
        "abort_requested",
        "detail_code",
    )
    return {key: outcome.get(key) for key in keys}


def check_payload_sha256(outcome: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(stable_check_payload(outcome)).encode("utf-8")).hexdigest()


def classification_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in CLASSIFICATIONS}
    for result in results:
        classification = result["classification"]
        counts[classification] = counts.get(classification, 0) + 1
    return dict(sorted(counts.items()))


def _quantile_fixed(
    values: list[Decimal],
    probability: str,
    scale: int,
    quantity: str,
) -> dict[str, Any]:
    ordered = sorted(values)
    p = dec(probability)
    index = max(0, ceil_decimal(p * len(ordered)) - 1)
    return fixed_record(ordered[index], scale, quantity=quantity)


def _ramp_ratio(plan: Plan, state: str, recovery_age: int) -> Decimal:
    curve = plan.slow_start
    if state == "UNHEALTHY":
        return Decimal("0")
    if state == "SUSPECT":
        return curve.suspect_ratio
    if recovery_age >= curve.ramp_steps:
        return Decimal("1")
    progress = Decimal(recovery_age) / Decimal(curve.ramp_steps)
    if curve.curve == "power":
        progress = progress**curve.exponent
    return curve.initial_ratio + (Decimal("1") - curve.initial_ratio) * progress


def effective_weight(
    instance: InstanceConfig,
    plan: Plan,
    state: str,
    recovery_age: int,
    score: Decimal,
) -> tuple[Decimal, Decimal]:
    factor = _ramp_ratio(plan, state, recovery_age)
    return factor, Decimal(instance.weight) * factor * score


def initial_state_row(instance: InstanceConfig, plan: Plan, scale: int) -> dict[str, Any]:
    state = plan.initial_state
    recovery_age = plan.slow_start.ramp_steps if state == "HEALTHY" else 0
    factor, weight = effective_weight(instance, plan, state, recovery_age, Decimal("1"))
    weight_fixed, _ = quantize_fixed(weight, scale)
    return {
        "instance_id": instance.instance_id,
        "state": state,
        "consecutive_failures": 0,
        "consecutive_successes": 0,
        "recovery_age": recovery_age,
        "weight_factor_raw": factor,
        "effective_weight_raw": weight,
        "effective_weight_fixed": weight_fixed,
        "applied_check_count": 0,
        "last_check_index": -1,
        "logical_watermark_ms": -1,
    }


def advance_instance_state(
    state_row: dict[str, Any],
    instance: InstanceConfig,
    plan: Plan,
    window_results: list[dict[str, Any]],
    new_results: list[dict[str, Any]],
    success_rate: Decimal,
    score: Decimal,
    scale: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    previous_state = state_row["state"]
    consecutive_failures = int(state_row["consecutive_failures"])
    consecutive_successes = int(state_row["consecutive_successes"])
    for result in new_results:
        if result["classification"] == "success":
            consecutive_successes += 1
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            consecutive_successes = 0

    if previous_state == "HEALTHY":
        if (
            consecutive_failures >= instance.failure_threshold
            or success_rate < plan.healthy_success_rate
        ):
            next_state = "SUSPECT"
        else:
            next_state = "HEALTHY"
    elif previous_state == "SUSPECT":
        if (
            consecutive_failures >= instance.failure_threshold
            or success_rate < plan.suspect_success_rate
        ):
            next_state = "UNHEALTHY"
        elif (
            consecutive_successes >= instance.success_threshold
            and success_rate >= plan.recovery_success_rate
        ):
            next_state = "HEALTHY"
        else:
            next_state = "SUSPECT"
    else:
        if (
            consecutive_successes >= instance.success_threshold
            and success_rate >= plan.recovery_success_rate
        ):
            next_state = "SUSPECT"
        else:
            next_state = "UNHEALTHY"

    previous_age = int(state_row["recovery_age"])
    if next_state == "HEALTHY":
        if previous_state == "HEALTHY":
            recovery_age = min(previous_age + 1, plan.slow_start.ramp_steps)
        else:
            recovery_age = 0
    else:
        recovery_age = 0

    factor, weight = effective_weight(instance, plan, next_state, recovery_age, score)
    weight_fixed, _ = quantize_fixed(weight, scale)
    end_index = int(window_results[-1]["check_index"])
    new_row = {
        **state_row,
        "state": next_state,
        "consecutive_failures": consecutive_failures,
        "consecutive_successes": consecutive_successes,
        "recovery_age": recovery_age,
        "weight_factor_raw": factor,
        "effective_weight_raw": weight,
        "effective_weight_fixed": weight_fixed,
        "applied_check_count": end_index + 1,
        "last_check_index": end_index,
    }
    transition = {
        "previous_state": previous_state,
        "next_state": next_state,
        "consecutive_failures": consecutive_failures,
        "consecutive_successes": consecutive_successes,
        "window_success_rate": float(success_rate),
        "window_score": float(score),
        "recovery_age": recovery_age,
    }
    return new_row, transition


def build_window_payload(
    config: WorkbenchConfig,
    instance: InstanceConfig,
    window_index: int,
    state_row: dict[str, Any],
    window_results: list[dict[str, Any]],
    new_results: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = config.plan
    scale = config.diagnostics.fixed_scale
    count = len(window_results)
    if count <= 0:
        raise ValueError("window cannot be empty")
    counts = classification_counts(window_results)
    success_count = counts["success"]
    success_rate = Decimal(success_count) / Decimal(count)
    score = sum(
        plan.score_weights.for_class(name) * quantity for name, quantity in counts.items()
    ) / Decimal(count)
    quality_values = [
        plan.score_weights.for_class(result["classification"]) for result in window_results
    ]
    rate_record = fixed_record(success_rate, scale, quantity="success_rate")
    score_record = fixed_record(score, scale, quantity="health_score")
    controlled_rate = fixed_to_decimal(rate_record["fixed"], scale)
    controlled_score = fixed_to_decimal(score_record["fixed"], scale)
    new_row, transition = advance_instance_state(
        state_row,
        instance,
        plan,
        window_results,
        new_results,
        controlled_rate,
        controlled_score,
        scale,
    )
    factor = dec(new_row["weight_factor_raw"])
    weight = dec(new_row["effective_weight_raw"])
    factor_record = fixed_record(factor, scale, quantity="slow_start_factor")
    weight_record = fixed_record(weight, scale, quantity="effective_weight")
    quantiles = {
        "p50": _quantile_fixed(quality_values, "0.50", scale, "quality_p50"),
        "p95": _quantile_fixed(quality_values, "0.95", scale, "quality_p95"),
    }
    source_hashes = [check_payload_sha256(result) for result in window_results]
    payload = {
        "instance_id": instance.instance_id,
        "window_index": window_index,
        "window_version": f"v1:{config.sample_sha256}",
        "sample_sha256": config.sample_sha256,
        "start_check_index": int(window_results[0]["check_index"]),
        "end_check_index": int(window_results[-1]["check_index"]),
        "window_start_ms": int(window_results[0]["scheduled_at_ms"]),
        "window_end_ms": int(window_results[-1]["scheduled_at_ms"]),
        "check_count": count,
        "success_count": success_count,
        "failure_count": count - success_count,
        "class_counts": counts,
        "success_rate_raw": float(success_rate),
        "success_rate_fixed": rate_record,
        "health_score_raw": float(score),
        "health_score_fixed": score_record,
        "quality_quantiles": quantiles,
        "slow_start_factor_raw": float(factor),
        "slow_start_factor_fixed": factor_record,
        "effective_weight_raw": float(weight),
        "effective_weight_fixed": weight_record,
        "state_before": state_row["state"],
        "state_after": new_row["state"],
        "transition": transition,
        "consecutive_failures": new_row["consecutive_failures"],
        "consecutive_successes": new_row["consecutive_successes"],
        "recovery_age": new_row["recovery_age"],
        "applied_check_count": new_row["applied_check_count"],
        "new_check_count": len(new_results),
        "rounding_mode": ROUNDING_MODE,
        "rounding_trail": [
            rate_record,
            score_record,
            factor_record,
            weight_record,
        ],
        "source_check_hashes": source_hashes,
        "source_sha256": hashlib.sha256(
            canonical_json(source_hashes).encode("utf-8")
        ).hexdigest(),
    }
    return payload, new_row


def _raw_decimal(record: dict[str, Any]) -> Decimal:
    return dec(record["raw_decimal"])


def build_sensitivity_payload(
    config: WorkbenchConfig,
    window_a: dict[str, Any],
    window_b: dict[str, Any],
    window_index: int,
) -> dict[str, Any]:
    scale = config.diagnostics.fixed_scale
    tolerance = config.diagnostics.float_tolerance
    instance_a = config.instance(window_a["instance_id"])
    instance_b = config.instance(window_b["instance_id"])
    score_a = _raw_decimal(window_a["health_score_fixed"])
    score_b = _raw_decimal(window_b["health_score_fixed"])
    score_diff = abs(score_a - score_b)
    weight_a = _raw_decimal(window_a["effective_weight_fixed"])
    weight_b = _raw_decimal(window_b["effective_weight_fixed"])
    margin = abs(weight_a - weight_b)
    factor_a = _raw_decimal(window_a["slow_start_factor_fixed"])
    factor_b = _raw_decimal(window_b["slow_start_factor_fixed"])
    quantum = Decimal("1") / Decimal(scale)
    score_error_bound = tolerance + quantum
    weight_error_bound = (
        Decimal(instance_a.weight) * factor_a
        + Decimal(instance_b.weight) * factor_b
    ) * score_error_bound + Decimal(2) * quantum
    both_allocating = weight_a > 0 and weight_b > 0
    within_tolerance = score_diff < tolerance
    nonzero_delta = score_diff > 0
    allocation_depends = (
        both_allocating and nonzero_delta and margin <= weight_error_bound
    )
    sensitive = within_tolerance and allocation_depends
    pair_id = f"{instance_a.instance_id}|{instance_b.instance_id}|w{window_index}"
    reasons: list[str] = []
    if not within_tolerance:
        reasons.append("SCORE_DIFFERENCE_OUTSIDE_TOLERANCE")
    if not both_allocating:
        reasons.append("ZERO_ALLOCATION_WEIGHT")
    if not nonzero_delta:
        reasons.append("IDENTICAL_SCORES_NO_DELTA")
    if both_allocating and not allocation_depends:
        reasons.append("ORDERING_INDEPENDENT_OF_SCORE_DIFFERENCE")
    if not reasons:
        reasons.append("SCORE_DIFFERENCE_CONTROLS_ALLOCATION_ORDER")
    diff_record = fixed_record(score_diff, scale, quantity="score_difference")
    margin_record = fixed_record(margin, scale, quantity="effective_weight_ordering_margin")
    score_bound_record = fixed_record(
        score_error_bound, scale, quantity="score_error_upper_bound"
    )
    weight_bound_record = fixed_record(
        weight_error_bound, scale, quantity="effective_weight_error_upper_bound"
    )
    chain = [
        {
            "step": "instance_a_score",
            "instance_id": instance_a.instance_id,
            "window_index": window_index,
            **window_a["health_score_fixed"],
        },
        {
            "step": "instance_b_score",
            "instance_id": instance_b.instance_id,
            "window_index": window_index,
            **window_b["health_score_fixed"],
        },
        {"step": "score_difference", **diff_record},
        {
            "step": "tolerance_compare",
            "tolerance_raw_float": float(tolerance),
            "tolerance_raw_decimal": canonical_decimal(tolerance),
            "within_tolerance": within_tolerance,
        },
        {"step": "score_error_upper_bound", **score_bound_record},
        {
            "step": "instance_a_effective_weight",
            "instance_id": instance_a.instance_id,
            **window_a["effective_weight_fixed"],
        },
        {
            "step": "instance_b_effective_weight",
            "instance_id": instance_b.instance_id,
            **window_b["effective_weight_fixed"],
        },
        {"step": "ordering_margin", **margin_record},
        {"step": "allocation_error_upper_bound", **weight_bound_record},
        {
            "step": "allocation_dependency",
            "depends_on_score_difference": allocation_depends,
            "both_allocating": both_allocating,
        },
        {
            "step": "verdict",
            "precision_sensitive": sensitive,
            "reasons": reasons,
        },
    ]
    return {
        "pair_id": pair_id,
        "instance_a": instance_a.instance_id,
        "instance_b": instance_b.instance_id,
        "window_index": window_index,
        "window_version": window_a["window_version"],
        "sample_sha256": config.sample_sha256,
        "precision_sensitive": sensitive,
        "within_tolerance": within_tolerance,
        "allocation_depends_on_score_difference": allocation_depends,
        "score_a": window_a["health_score_fixed"],
        "score_b": window_b["health_score_fixed"],
        "score_difference": diff_record,
        "tolerance": {
            "raw_float": float(tolerance),
            "raw_decimal": canonical_decimal(tolerance),
        },
        "effective_weight_a": window_a["effective_weight_fixed"],
        "effective_weight_b": window_b["effective_weight_fixed"],
        "ordering_margin": margin_record,
        "cumulative_error_upper_bound": {
            "score_units": score_bound_record,
            "effective_weight_units": weight_bound_record,
        },
        "reasons": reasons,
        "error_source_chain": chain,
    }


def sensitivity_payload_sha256(payload: dict[str, Any]) -> str:
    body = dict(payload)
    body.pop("payload_sha256", None)
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def fixed_weight_display(value: int, scale: int) -> dict[str, Any]:
    return {
        "fixed": value,
        "scale": scale,
        "value": float(fixed_to_decimal(value, scale)),
    }

