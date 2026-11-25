from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Sequence

from .aggregate import estimate_logical_ms
from .errors import ErrorCode
from .fixedpoint import ErrorLedger, FixedValue, frac_str
from .model import FormSpec, PlanSpec, RuntimeConfig, WindowSpec


@dataclass(frozen=True)
class WindowObservation:
    table_name: str
    window_id: str
    clock_ms: int
    batch_seqs: tuple[int, ...]
    batch_digests: tuple[str, ...]
    input_rows: int
    output_cardinality: int
    elapsed_ms: Fraction

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "window_id": self.window_id,
            "clock_ms": self.clock_ms,
            "batch_seqs": list(self.batch_seqs),
            "batch_digests": list(self.batch_digests),
            "input_rows": self.input_rows,
            "output_cardinality": self.output_cardinality,
            "elapsed_ms": frac_str(self.elapsed_ms),
        }


def relative_deviation(observed: Fraction | int, estimated: Fraction | int) -> Fraction:
    estimated = Fraction(estimated)
    if estimated <= 0:
        raise ValueError("estimated cost must be positive")
    return abs(Fraction(observed) - estimated) / estimated


def evaluate_drift(
    observation: WindowObservation,
    window: WindowSpec,
    config: RuntimeConfig,
    ledger: ErrorLedger,
    *,
    last_trigger_clock_ms: int | None,
    observation_count: int,
) -> dict[str, Any]:
    estimate = window.cost_estimate
    metrics = {
        "input_rows": (Fraction(observation.input_rows), Fraction(estimate.input_rows)),
        "output_cardinality": (
            Fraction(observation.output_cardinality),
            Fraction(estimate.output_cardinality),
        ),
        "elapsed_ms": (observation.elapsed_ms, estimate.elapsed_ms),
    }
    metric_payload: dict[str, Any] = {}
    max_deviation = Fraction(0)
    primary_metric = None
    fixed_subjects: list[FixedValue] = []
    for metric_name, (observed_value, estimated_value) in metrics.items():
        observed_fixed = ledger.convert(
            observed_value,
            subject=f"{observation.window_id}:{metric_name}:observed",
            kind=metric_name,
            source_batches=observation.batch_seqs,
        )
        estimated_fixed = ledger.convert(
            estimated_value,
            subject=f"{observation.window_id}:{metric_name}:estimated",
            kind=f"{metric_name}_estimate",
        )
        deviation = relative_deviation(observed_value, estimated_value)
        deviation_fixed = ledger.convert(
            deviation,
            subject=f"{observation.window_id}:{metric_name}:deviation",
            kind="deviation_ratio",
            source_batches=observation.batch_seqs,
        )
        fixed_subjects.extend([observed_fixed, estimated_fixed, deviation_fixed])
        metric_payload[metric_name] = {
            "observed": frac_str(observed_value),
            "estimated": frac_str(estimated_value),
            "deviation_ratio": frac_str(deviation),
            "deviation_ratio_decimal": float(deviation),
            "exceeds_threshold": deviation >= config.drift_threshold,
            "observed_conversion_id": observed_fixed.conversion_id,
            "estimated_conversion_id": estimated_fixed.conversion_id,
            "deviation_conversion_id": deviation_fixed.conversion_id,
        }
        if deviation > max_deviation:
            max_deviation = deviation
            primary_metric = metric_name

    enough_batches = observation_count >= config.min_observation_batches
    cooldown_remaining = 0
    cooldown_ok = True
    if last_trigger_clock_ms is not None:
        cooldown_remaining = max(
            0,
            config.cooldown_ms - max(0, observation.clock_ms - last_trigger_clock_ms),
        )
        cooldown_ok = observation.clock_ms - last_trigger_clock_ms >= config.cooldown_ms
    threshold_exceeded = max_deviation >= config.drift_threshold
    triggered = enough_batches and threshold_exceeded and cooldown_ok
    basis = [
        f"{name}: observed={payload['observed']}, estimated={payload['estimated']}, "
        f"deviation={payload['deviation_ratio']} "
        f"{'>=' if payload['exceeds_threshold'] else '<'} threshold={frac_str(config.drift_threshold)}"
        for name, payload in metric_payload.items()
        if payload["exceeds_threshold"]
    ]
    if not basis and threshold_exceeded:
        basis.append(f"{primary_metric} deviation {frac_str(max_deviation)} reaches threshold")
    if not enough_batches:
        basis.append(f"observation_count={observation_count} below min_observation_batches")
    if not cooldown_ok:
        basis.append(f"cooldown active; remaining_logical_ms={cooldown_remaining}")

    event_key = f"drift:{observation.window_id}:{observation.batch_seqs[0] if observation.batch_seqs else 0}-{observation.batch_seqs[-1] if observation.batch_seqs else 0}"
    return {
        "event_key": event_key,
        "triggered": triggered,
        "table_name": observation.table_name,
        "window_id": observation.window_id,
        "primary_metric": primary_metric,
        "deviation_ratio": frac_str(max_deviation),
        "deviation_ratio_decimal": float(max_deviation),
        "threshold": frac_str(config.drift_threshold),
        "threshold_decimal": float(config.drift_threshold),
        "threshold_headroom": frac_str(config.drift_threshold - max_deviation),
        "threshold_excess": frac_str(max_deviation - config.drift_threshold),
        "batch_start_seq": observation.batch_seqs[0] if observation.batch_seqs else None,
        "batch_end_seq": observation.batch_seqs[-1] if observation.batch_seqs else None,
        "used_observation_batch_interval": list(observation.batch_seqs),
        "batch_digests": list(observation.batch_digests),
        "clock_ms": observation.clock_ms,
        "cooldown_ms": config.cooldown_ms,
        "cooldown_ok": cooldown_ok,
        "cooldown_remaining_ms": cooldown_remaining,
        "observation_count": observation_count,
        "basis": basis,
        "metrics": metric_payload,
        "error_chain": ledger.source_chain(fixed_subjects),
    }


def structural_switch_cost(
    current: FormSpec,
    candidate: FormSpec,
    plan: PlanSpec,
) -> Fraction:
    cost = Fraction(2)
    changed_operators = 0
    for window_id in plan.window_order:
        old_op = current.operators.get(window_id, plan.window(window_id).operator)
        new_op = candidate.operators.get(window_id, old_op)
        if old_op != new_op:
            changed_operators += 1
    cost += changed_operators * 4
    if tuple(current.window_order) != tuple(candidate.window_order):
        inversions = 0
        old_pos = {window_id: index for index, window_id in enumerate(current.window_order)}
        for left, right in zip(candidate.window_order, candidate.window_order[1:]):
            if old_pos.get(left, 0) > old_pos.get(right, 0):
                inversions += 1
        cost += Fraction(6) + inversions
    changed_group_orders = 0
    for window_id, order in candidate.group_orders.items():
        window = plan.window(window_id)
        old_order = current.group_orders.get(window_id, window.group_by)
        if tuple(old_order) != tuple(order):
            changed_group_orders += 1
    cost += changed_group_orders * 3
    return cost


def fallback_switch_cost(
    current: FormSpec,
    candidate: FormSpec,
    plan: PlanSpec,
    latest_observation: WindowObservation | None,
) -> Fraction:
    cost = structural_switch_cost(current, candidate, plan)
    if latest_observation is not None:
        cost += Fraction(latest_observation.input_rows, 100)
        cost += Fraction(latest_observation.output_cardinality, 10)
    return cost


def projected_runtime(form: FormSpec, plan: PlanSpec) -> Fraction:
    total = Fraction(0)
    for window in plan.windows:
        operator = form.operators.get(window.window_id, window.operator)
        if form.projected_runtime_ms is not None and len(plan.windows) == 1:
            return form.projected_runtime_ms
        total += estimate_logical_ms(
            operator,
            window.cost_estimate.input_rows,
            window.cost_estimate.output_cardinality,
        )
    if form.projected_runtime_ms is not None:
        return form.projected_runtime_ms
    return total


def _candidate_costs(
    current: FormSpec,
    candidate: FormSpec,
    plan: PlanSpec,
    observation: WindowObservation | None,
    ledger: ErrorLedger,
) -> dict[str, Any]:
    runtime_exact = candidate.projected_runtime_ms
    if runtime_exact is None:
        runtime_exact = projected_runtime(candidate, plan)
    source_batches = observation.batch_seqs if observation else ()
    component_values: list[FixedValue] = []
    if candidate.switch_cost_ms is not None:
        switch_exact = candidate.switch_cost_ms
        switch_fixed = ledger.convert(
            switch_exact,
            subject=f"{candidate.form_id}:switch_cost",
            kind="switch_cost",
            rounding="ceil",
            source_batches=source_batches,
        )
    else:
        setup_exact = candidate.setup_cost_ms
        if setup_exact is None:
            setup_exact = structural_switch_cost(current, candidate, plan)
        setup_fixed = ledger.convert(
            setup_exact,
            subject=f"{candidate.form_id}:switch_setup",
            kind="switch_setup_cost",
            rounding="ceil",
        )
        component_values.append(setup_fixed)
        switch_exact = setup_exact
        if observation is not None:
            row_count_fixed = ledger.convert(
                observation.input_rows,
                subject=f"{candidate.form_id}:migration_rows",
                kind="observed_window_rows",
                source_batches=source_batches,
            )
            row_rate_fixed = ledger.convert(
                Fraction(1, 100),
                subject=f"{candidate.form_id}:row_rate_ms",
                kind="row_rate",
            )
            row_component_fixed = ledger.multiply(
                row_count_fixed,
                row_rate_fixed,
                subject=f"{candidate.form_id}:row_migration_cost",
            )
            cardinality_fixed = ledger.convert(
                observation.output_cardinality,
                subject=f"{candidate.form_id}:migration_groups",
                kind="observed_output_cardinality",
                source_batches=source_batches,
            )
            cardinality_rate_fixed = ledger.convert(
                Fraction(1, 10),
                subject=f"{candidate.form_id}:group_rate_ms",
                kind="group_rate",
            )
            cardinality_component_fixed = ledger.multiply(
                cardinality_fixed,
                cardinality_rate_fixed,
                subject=f"{candidate.form_id}:cardinality_migration_cost",
            )
            data_exact = Fraction(observation.input_rows, 100) + Fraction(
                observation.output_cardinality, 10
            )
            switch_exact += data_exact
            component_values.extend([row_component_fixed, cardinality_component_fixed])
        switch_fixed = ledger.add(component_values, subject=f"{candidate.form_id}:switch_cost")
    runtime_fixed = ledger.convert(
        runtime_exact,
        subject=f"{candidate.form_id}:projected_runtime",
        kind="projected_runtime",
        rounding="ceil",
        source_batches=source_batches,
    )
    total_fixed = ledger.add([switch_fixed, runtime_fixed], subject=f"{candidate.form_id}:total_cost")
    return {
        "switch_exact": switch_exact,
        "runtime_exact": runtime_exact,
        "total_exact": switch_exact + runtime_exact,
        "switch_fixed": switch_fixed,
        "runtime_fixed": runtime_fixed,
        "total_fixed": total_fixed,
    }


def adjudicate_switch(
    current: FormSpec,
    candidates: Sequence[FormSpec],
    plan: PlanSpec,
    config: RuntimeConfig,
    ledger: ErrorLedger,
    *,
    observation: WindowObservation | None,
    drift_event: dict[str, Any] | None,
    requested_version: int | None = None,
) -> dict[str, Any]:
    current_runtime = projected_runtime(current, plan)
    budget_fixed = ledger.convert(config.switch_cost_budget_ms, subject="switch_budget", kind="switch_budget")
    assessments: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    feasible: list[dict[str, Any]] = []

    for candidate in candidates:
        costs = _candidate_costs(current, candidate, plan, observation, ledger)
        assessment = {
            "form_id": candidate.form_id,
            "requested_target_version": candidate.target_version or requested_version,
            "switch_cost_ms": frac_str(costs["switch_exact"]),
            "projected_runtime_ms": frac_str(costs["runtime_exact"]),
            "total_estimated_cost_ms": frac_str(costs["total_exact"]),
            "fixed_switch_integer": costs["switch_fixed"].integer,
            "fixed_total_integer": costs["total_fixed"].integer,
            "error_bound": frac_str(costs["total_fixed"].error_bound),
            "source_chain": ledger.source_chain(
                [costs["switch_fixed"], costs["runtime_fixed"], costs["total_fixed"]]
            ),
        }
        assessments.append(assessment)
        if costs["switch_exact"] > config.switch_cost_budget_ms:
            item = {
                **assessment,
                "first_rejection_reason": ErrorCode.SWITCH_BUDGET_INSUFFICIENT,
                "reason_detail": (
                    f"switch cost {frac_str(costs['switch_exact'])}ms exceeds budget "
                    f"{frac_str(config.switch_cost_budget_ms)}ms"
                ),
            }
            rejected.append(item)
            continue
        feasible.append({"candidate": candidate, "costs": costs, "assessment": assessment})

    if not feasible:
        budget_rejections = [item for item in rejected if item["first_rejection_reason"] == ErrorCode.SWITCH_BUDGET_INSUFFICIENT]
        code = ErrorCode.SWITCH_BUDGET_INSUFFICIENT if budget_rejections else ErrorCode.CANDIDATE_INVALID
        return {
            "switched": False,
            "status": "rejected_budget" if budget_rejections else "rejected_invalid",
            "error_code": code,
            "message": "no execution form can be switched within the switching cost budget",
            "current_form_id": current.form_id,
            "current_projected_runtime_ms": frac_str(current_runtime),
            "budget_ms": frac_str(config.switch_cost_budget_ms),
            "budget_fixed_integer": budget_fixed.integer,
            "assessments": assessments,
            "rejected_candidates": rejected,
            "precision_sensitive": False,
            "drift_event_key": drift_event.get("event_key") if drift_event else None,
        }

    feasible.sort(key=lambda item: (item["costs"]["total_exact"], item["candidate"].form_id))
    best = feasible[0]
    best_total = best["costs"]["total_exact"]
    if best_total >= current_runtime:
        rejected.append(
            {
                **best["assessment"],
                "first_rejection_reason": ErrorCode.SWITCH_NO_IMPROVEMENT,
                "reason_detail": (
                    f"candidate total {frac_str(best_total)}ms is not lower than current projected "
                    f"runtime {frac_str(current_runtime)}ms"
                ),
            }
        )
        return {
            "switched": False,
            "status": "rejected_no_improvement",
            "error_code": ErrorCode.SWITCH_NO_IMPROVEMENT,
            "message": "re-optimization found no estimated improvement; preserving form pointer",
            "current_form_id": current.form_id,
            "current_projected_runtime_ms": frac_str(current_runtime),
            "assessments": assessments,
            "rejected_candidates": rejected,
            "precision_sensitive": False,
            "drift_event_key": drift_event.get("event_key") if drift_event else None,
        }

    for item in feasible[1:]:
        rejected.append(
            {
                **item["assessment"],
                "first_rejection_reason": "HIGHER_TOTAL_COST",
                "reason_detail": (
                    f"total {frac_str(item['costs']['total_exact'])}ms is not lower than selected "
                    f"{frac_str(best_total)}ms"
                ),
            }
        )

    precision_flags: list[dict[str, Any]] = []
    nearest = None
    if len(feasible) > 1:
        nearest = min(
            feasible[1:],
            key=lambda item: abs(item["costs"]["total_exact"] - best_total),
        )
        difference = abs(nearest["costs"]["total_exact"] - best_total)
        fixed_difference = abs(nearest["costs"]["total_fixed"].integer - best["costs"]["total_fixed"].integer)
        chain = ledger.source_chain(
            [best["costs"]["total_fixed"], nearest["costs"]["total_fixed"]]
        )
        if difference < config.cost_tolerance_ms:
            precision_flags.append(
                {
                    "flag_key": f"precision:{best['candidate'].form_id}:{nearest['candidate'].form_id}",
                    "candidate_a": best["candidate"].form_id,
                    "candidate_b": nearest["candidate"].form_id,
                    "exact_cost_difference_ms": frac_str(difference),
                    "tolerance_ms": frac_str(config.cost_tolerance_ms),
                    "fixed_integer_difference": fixed_difference,
                    "scale": config.fixed_point_scale,
                    "decision_depends_on_difference": True,
                    "selected_form_id": best["candidate"].form_id,
                    "error_chain": chain,
                    "error_upper_bound": chain["error_upper_bound"],
                }
            )

    return {
        "switched": True,
        "status": "selected",
        "error_code": None,
        "selected_form": best["candidate"],
        "selected_assessment": best["assessment"],
        "current_form_id": current.form_id,
        "current_projected_runtime_ms": frac_str(current_runtime),
        "selected_total_cost_ms": frac_str(best_total),
        "requested_version": requested_version,
        "assessments": assessments,
        "rejected_candidates": rejected,
        "precision_sensitive": bool(precision_flags),
        "precision_flags": precision_flags,
        "drift_event_key": drift_event.get("event_key") if drift_event else None,
    }
