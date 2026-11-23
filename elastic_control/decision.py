"""Metric-to-replica decisioning with deterministic tie breaking.

The order is fixed:

1. evaluate whether a rule is triggered by the metric aggregate;
2. compute a metric ratio and the rule's minimum movement;
3. round up (scale out) or down (scale in);
4. clamp to ``[min_replicas, max_replicas]``.

Multiple triggered rules are ordered by descending priority, then scale-out
over scale-in for safety, then candidate magnitude.  Every losing rule carries
a stable first-rejection reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from typing import Any, Mapping

from .aggregation import WindowAggregate, latest_window_per_metric
from .fixedpoint import Divergence, FixedPointBook
from .models import MetricSource, PolicyBundle, ScaleRule, Workload
from .util import DECIMAL_CONTEXT_PRECISION, dec, decimal_text


@dataclass(frozen=True)
class FixedInputs:
    metric: str
    scale: int
    aggregate_units: int
    target_units: int
    aggregate_step_id: str
    target_step_id: str
    aggregate_step_no: int
    target_step_no: int
    aggregate_error_bound: Decimal
    target_error_bound: Decimal


@dataclass
class RuleEvaluation:
    rule: ScaleRule
    metric: MetricSource
    triggered: bool
    trigger_reason: str
    direction: str | None
    aggregate: Decimal | None
    threshold: Decimal | None
    ideal_raw: Decimal | None
    movement_raw: Decimal | None
    score: Decimal | None
    candidate_raw_rounded: int | None
    candidate_fixed: int | None
    target_replicas: int | None
    clamped: bool
    error_bound: Decimal
    fixed_divergence: Divergence | None = None
    high_scale_candidate: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rule": self.rule.name,
            "metric": self.metric.name,
            "triggered": self.triggered,
            "trigger_reason": self.trigger_reason,
            "direction": self.direction,
            "priority": self.rule.priority,
            "step": self.rule.step,
            "percent": decimal_text(self.rule.percent),
            "error_bound": decimal_text(self.error_bound),
            "clamped": self.clamped,
        }
        optional = {
            "aggregate": self.aggregate,
            "threshold": self.threshold,
            "ideal_raw": self.ideal_raw,
            "movement_raw": self.movement_raw,
            "decision_score": self.score,
            "candidate_raw_rounded": self.candidate_raw_rounded,
            "candidate_fixed": self.candidate_fixed,
            "target_replicas": self.target_replicas,
            "high_scale_candidate": self.high_scale_candidate,
        }
        for key, value in optional.items():
            if value is not None:
                if isinstance(value, Decimal):
                    result[key] = decimal_text(value)
                else:
                    result[key] = value
        if self.fixed_divergence is not None:
            result["fixed_decimal_divergence"] = self.fixed_divergence.to_dict()
        return result


@dataclass
class WorkloadDecision:
    workload: str
    current_replicas: int
    target_replicas: int
    selected_rule: str | None
    selected_direction: str | None
    selected_priority: int | None
    selected_version: int
    delta: int
    evaluations: list[RuleEvaluation]
    rejected_rules: list[dict[str, Any]]
    precision_sensitive: bool
    basis: dict[str, Any]
    cache_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "workload": self.workload,
            "current_replicas": self.current_replicas,
            "target_replicas": self.target_replicas,
            "delta": self.delta,
            "selected_rule": self.selected_rule,
            "selected_direction": self.selected_direction,
            "selected_priority": self.selected_priority,
            "selected_rule_version": self.selected_version,
            "precision_sensitive": self.precision_sensitive,
            "rejected_rules": self.rejected_rules,
            "evaluations": [item.to_dict() for item in self.evaluations],
            "basis": self.basis,
            "cache_key": self.cache_key,
        }


@dataclass
class PrecisionFlag:
    workload: str
    flag_type: str
    rules: list[str]
    message: str
    score_gap: Decimal | None = None
    tolerance: Decimal | None = None
    error_bound: Decimal | None = None
    higher_scale: int | None = None
    would_flip_at_higher_scale: bool | None = None
    metrics: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workload": self.workload,
            "type": self.flag_type,
            "rules": self.rules,
            "metrics": self.metrics,
            "message": self.message,
            "score_gap": decimal_text(self.score_gap) if self.score_gap is not None else None,
            "tolerance": decimal_text(self.tolerance) if self.tolerance is not None else None,
            "error_bound": decimal_text(self.error_bound) if self.error_bound is not None else None,
            "higher_scale": self.higher_scale,
            "would_flip_at_higher_scale": self.would_flip_at_higher_scale,
            "steps": self.steps,
        }


@dataclass
class DecisionOutput:
    at_ms: int
    workloads: dict[str, WorkloadDecision]
    precision_flags: list[PrecisionFlag]
    first_divergence: Divergence | None
    windows: list[WindowAggregate]
    ledger: FixedPointBook

    def to_dict(self) -> dict[str, Any]:
        return {
            "at_ms": self.at_ms,
            "workloads": [
                self.workloads[name].to_dict() for name in sorted(self.workloads)
            ],
            "precision_flags": [
                flag.to_dict()
                for flag in sorted(
                    self.precision_flags,
                    key=lambda item: (item.workload, item.flag_type, item.rules),
                )
            ],
            "first_precision_divergence": (
                None if self.first_divergence is None else self.first_divergence.to_dict()
            ),
            "windows": [
                window.to_dict()
                for window in sorted(
                    self.windows,
                    key=lambda item: (item.metric, item.window_start_ms),
                )
            ],
            "ledger": self.ledger.to_dict(),
        }


def _resolve_direction(
    rule: ScaleRule,
    metric: MetricSource,
    aggregate: Decimal,
) -> tuple[str | None, str]:
    if rule.direction in {"up", "down"}:
        if rule.direction == "up":
            if aggregate >= metric.alert_threshold:
                return "up", "aggregate_at_or_above_alert_threshold"
            return None, "aggregate_below_alert_threshold"
        threshold = (
            metric.lower_threshold
            if metric.lower_threshold is not None
            else metric.alert_threshold
        )
        if aggregate <= threshold:
            return "down", "aggregate_at_or_below_down_threshold"
        return None, "aggregate_above_down_threshold"

    if aggregate >= metric.alert_threshold and aggregate >= metric.target_value:
        return "up", "auto_high_watermark"
    lower = metric.lower_threshold
    if lower is not None and aggregate <= lower:
        return "down", "auto_low_watermark"
    if lower is None and aggregate <= metric.alert_threshold and aggregate < metric.target_value:
        return "down", "auto_single_threshold_low_side"
    return None, "metric_inside_deadband"


def _movement(
    workload: Workload,
    rule: ScaleRule,
    direction: str,
) -> int:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        percentage = (
            Decimal(workload.current_replicas) * rule.percent / Decimal(100)
        )
        if direction == "up":
            percentage_units = int(percentage.to_integral_value(rounding=ROUND_CEILING))
        else:
            percentage_units = int(percentage.to_integral_value(rounding=ROUND_FLOOR))
    return rule.step + percentage_units


def _round_and_clamp(score: Decimal, workload: Workload, direction: str) -> tuple[int, bool]:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        if direction == "up":
            rounded = int(score.to_integral_value(rounding=ROUND_CEILING))
        else:
            rounded = int(score.to_integral_value(rounding=ROUND_FLOOR))
    clamped_value = min(max(rounded, workload.min_replicas), workload.max_replicas)
    return clamped_value, clamped_value != rounded


def _fixed_candidate(
    current: int,
    rule: ScaleRule,
    direction: str,
    aggregate_units: int,
    target_units: int,
    workload: Workload,
    *,
    scale_override: int | None = None,
    aggregate_value: Decimal | None = None,
    target_value: Decimal | None = None,
) -> tuple[int, Decimal, Decimal, bool]:
    """Return fixed candidate, fixed score, error bound and clamp flag."""

    if target_units <= 0:
        target_units = 1
    numerator = Decimal(current) * Decimal(aggregate_units)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        if scale_override is None:
            ideal_fixed = numerator / Decimal(target_units)
        else:
            # Re-quantise both operands at a higher scale to audit whether the
            # conclusion is an artefact of the declared scale.
            high_agg = int(
                ((aggregate_value or Decimal(0)) * Decimal(scale_override)).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            high_target = int(
                ((target_value or Decimal(0)) * Decimal(scale_override)).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            ideal_fixed = (
                Decimal(current) * Decimal(high_agg) / Decimal(max(high_target, 1))
            )
        movement = _movement(workload, rule, direction)
        if direction == "up":
            fixed_score = max(ideal_fixed, Decimal(current + movement))
            rounded = int(fixed_score.to_integral_value(rounding=ROUND_CEILING))
        else:
            fixed_score = min(ideal_fixed, Decimal(max(current - movement, 0)))
            rounded = int(fixed_score.to_integral_value(rounding=ROUND_FLOOR))
        clamped_target = min(max(rounded, workload.min_replicas), workload.max_replicas)
        clamped = clamped_target != rounded
        propagation = (
            abs(Decimal(current)) / Decimal(max(target_units, 1))
            + abs(numerator) / Decimal(max(target_units, 1)) ** 2
        )
    return clamped_target, fixed_score, propagation, clamped


def decide(
    bundle: PolicyBundle,
    windows: list[WindowAggregate],
    replicas: Mapping[str, int],
    *,
    at_ms: int,
    ledger: FixedPointBook | None = None,
    workload_filter: set[str] | None = None,
) -> DecisionOutput:
    if bundle.budget is None:
        raise ValueError("bundle must contain a budget")
    book = ledger or FixedPointBook()
    latest = latest_window_per_metric(windows)
    tolerance = bundle.budget.float_tolerance
    metrics_to_convert = {
        metric
        for metric in latest
        if metric in bundle.metrics
        and (workload_filter is None or any(
            rule.metric == metric and rule.workload in workload_filter
            for rule in bundle.rules.values()
        ))
    }
    fixed_inputs: dict[str, FixedInputs] = {}
    for metric_name in sorted(metrics_to_convert):
        metric = bundle.metrics[metric_name]
        aggregate = latest[metric_name].aggregate
        aggregate_units = book.convert(
            metric_name,
            aggregate,
            metric.scale,
            operation="window_aggregate",
            step_id=f"{metric_name}:window_aggregate",
        )
        target_units = book.convert(
            metric_name,
            metric.target_value,
            metric.scale,
            operation="target_value",
            step_id=f"{metric_name}:target_value",
        )
        agg_ledger = book.ledger(metric_name, metric.scale)
        agg_step_no = agg_ledger.entries[-2].step_no if len(agg_ledger.entries) >= 2 else agg_ledger.next_step_no - 2
        target_step_no = agg_ledger.entries[-1].step_no
        fixed_inputs[metric_name] = FixedInputs(
            metric=metric_name,
            scale=metric.scale,
            aggregate_units=aggregate_units,
            target_units=target_units,
            aggregate_step_id=f"{metric_name}:window_aggregate",
            target_step_id=f"{metric_name}:target_value",
            aggregate_step_no=agg_step_no,
            target_step_no=target_step_no,
            aggregate_error_bound=abs(aggregate - Decimal(aggregate_units) / Decimal(metric.scale)),
            target_error_bound=abs(metric.target_value - Decimal(target_units) / Decimal(metric.scale)),
        )
        # These comparisons produce the first evidence of a fixed/Decimal
        # intermediate divergence, as required by the error ledger contract.
        book.compare(
            metric_name,
            scale=metric.scale,
            fixed_units=aggregate_units,
            decimal_value=aggregate,
            tolerance=tolerance,
            operation="window_aggregate",
            step_id=f"{metric_name}:window_aggregate",
        )
        book.compare(
            metric_name,
            scale=metric.scale,
            fixed_units=target_units,
            decimal_value=metric.target_value,
            tolerance=tolerance,
            operation="target_value",
            step_id=f"{metric_name}:target_value",
        )

    decisions: dict[str, WorkloadDecision] = {}
    flags: list[PrecisionFlag] = []
    selected_workloads = (
        sorted(bundle.workloads)
        if workload_filter is None
        else sorted(name for name in workload_filter if name in bundle.workloads)
    )
    for workload_name in selected_workloads:
        workload = bundle.workloads[workload_name]
        current = replicas.get(workload_name, workload.current_replicas)
        evaluations: list[RuleEvaluation] = []
        for rule in bundle.rules_for_workload(workload_name):
            metric = bundle.metrics[rule.metric]
            window = latest.get(rule.metric)
            if window is None:
                evaluations.append(
                    RuleEvaluation(
                        rule=rule,
                        metric=metric,
                        triggered=False,
                        trigger_reason="no_closed_window",
                        direction=None,
                        aggregate=None,
                        threshold=None,
                        ideal_raw=None,
                        movement_raw=None,
                        score=None,
                        candidate_raw_rounded=None,
                        candidate_fixed=None,
                        target_replicas=None,
                        clamped=False,
                        error_bound=Decimal(0),
                    )
                )
                continue
            aggregate = window.aggregate
            direction, trigger_reason = _resolve_direction(rule, metric, aggregate)
            if direction is None:
                evaluations.append(
                    RuleEvaluation(
                        rule=rule,
                        metric=metric,
                        triggered=False,
                        trigger_reason=trigger_reason,
                        direction=None,
                        aggregate=aggregate,
                        threshold=(
                            metric.alert_threshold
                            if rule.direction != "down"
                            else (
                                metric.lower_threshold
                                if metric.lower_threshold is not None
                                else metric.alert_threshold
                            )
                        ),
                        ideal_raw=None,
                        movement_raw=None,
                        score=None,
                        candidate_raw_rounded=None,
                        candidate_fixed=None,
                        target_replicas=None,
                        clamped=False,
                        error_bound=Decimal(0),
                    )
                )
                continue
            fixed = fixed_inputs[metric.name]
            with localcontext() as ctx:
                ctx.prec = DECIMAL_CONTEXT_PRECISION
                ideal_raw = Decimal(current) * aggregate / metric.target_value
                movement_value = _movement(workload, rule, direction)
                if direction == "up":
                    score = max(ideal_raw, Decimal(current + movement_value))
                else:
                    score = min(ideal_raw, Decimal(max(current - movement_value, 0)))
            rounded, clamped = _round_and_clamp(score, workload, direction)
            fixed_candidate, fixed_score, _, fixed_clamped = _fixed_candidate(
                current,
                rule,
                direction,
                fixed.aggregate_units,
                fixed.target_units,
                workload,
            )
            higher_scale = metric.scale * 10
            high_candidate, _, _, _ = _fixed_candidate(
                current,
                rule,
                direction,
                fixed.aggregate_units,
                fixed.target_units,
                workload,
                scale_override=higher_scale,
                aggregate_value=aggregate,
                target_value=metric.target_value,
            )
            product_units = book.record_product(
                metric.name,
                left_name="current_replicas",
                left=current,
                left_error=Decimal(0),
                right_name="aggregate_fixed_units",
                right=fixed.aggregate_units,
                right_error=fixed.aggregate_error_bound,
                scale=metric.scale,
                operation="ratio_numerator",
                step_id=f"{metric.name}:ratio_numerator:{rule.name}",
            )
            product_error_increment = dec(
                book.ledger(metric.name, metric.scale).entries[-1].error_increment
            )
            with localcontext() as ctx:
                ctx.prec = DECIMAL_CONTEXT_PRECISION
                ratio_error_increment = (
                    product_error_increment / Decimal(fixed.target_units)
                    + abs(Decimal(product_units))
                    / (Decimal(fixed.target_units) ** 2)
                    * fixed.target_error_bound
                )
            book.record_operation_error(
                metric.name,
                operation="replica_ratio",
                error_increment=ratio_error_increment,
                scale=metric.scale,
                step_id=f"{metric.name}:replica_ratio:{rule.name}",
                detail={
                    "workload": workload_name,
                    "rule": rule.name,
                    "numerator_units": product_units,
                    "denominator_units": fixed.target_units,
                },
            )
            book.record_result(
                metric.name,
                operation="target_replicas",
                input_value=score,
                fixed_units=fixed_candidate * metric.scale,
                scale=metric.scale,
                rounding="ceil" if direction == "up" else "floor",
                discarded=score - Decimal(fixed_candidate),
                step_id=f"{metric.name}:target_replicas:{rule.name}",
                detail={
                    "workload": workload_name,
                    "rule": rule.name,
                    "direction": direction,
                    "clamped": clamped or fixed_clamped,
                },
            )
            divergence = book.compare(
                metric.name,
                scale=metric.scale,
                fixed_units=fixed_candidate * metric.scale,
                decimal_value=Decimal(rounded),
                tolerance=tolerance,
                operation="target_replicas",
                step_id=fixed.aggregate_step_id,
                record=False,
            )
            if (
                divergence is None
                and rounded != fixed_candidate
                and abs(Decimal(fixed_candidate - rounded)) > tolerance
            ):
                divergence = Divergence(
                    metric=metric.name,
                    step_no=fixed.aggregate_step_no,
                    step_id=fixed.aggregate_step_id,
                    fixed_units=fixed_candidate,
                    fixed_value=Decimal(fixed_candidate),
                    decimal_value=Decimal(rounded),
                    difference=abs(Decimal(fixed_candidate - rounded)),
                    tolerance=tolerance,
                    operation="target_replicas",
                    message=(
                        f"fixed-point and Decimal target rounding differ for "
                        f"workload {workload_name!r} rule {rule.name!r}"
                    ),
                )
            if divergence is None:
                divergence = next(
                    (
                        item
                        for item in book.divergences
                        if item.metric == metric.name
                    ),
                    None,
                )
            if divergence is not None:
                book.divergences.append(divergence)
            error_bound = (
                fixed.aggregate_error_bound
                + fixed.target_error_bound
                + product_error_increment
                + ratio_error_increment
            )
            evaluations.append(
                RuleEvaluation(
                    rule=rule,
                    metric=metric,
                    triggered=True,
                    trigger_reason=trigger_reason,
                    direction=direction,
                    aggregate=aggregate,
                    threshold=(
                        metric.alert_threshold
                        if direction == "up"
                        else (
                            metric.lower_threshold
                            if metric.lower_threshold is not None
                            else metric.alert_threshold
                        )
                    ),
                    ideal_raw=ideal_raw,
                    movement_raw=Decimal(movement_value),
                    score=score,
                    candidate_raw_rounded=rounded,
                    candidate_fixed=fixed_candidate,
                    target_replicas=fixed_candidate,
                    clamped=clamped or fixed_clamped,
                    error_bound=error_bound,
                    fixed_divergence=divergence,
                    high_scale_candidate=high_candidate,
                )
            )

        triggered = [evaluation for evaluation in evaluations if evaluation.triggered]
        selected: RuleEvaluation | None = None
        rejected: list[dict[str, Any]] = []
        if triggered:
            highest_priority = max(item.rule.priority for item in triggered)
            priority_group = [
                item for item in triggered if item.rule.priority == highest_priority
            ]
            up_group = [item for item in priority_group if item.direction == "up"]
            if up_group:
                selected = min(
                    up_group,
                    key=lambda item: (-(item.score or Decimal(0)), item.rule.name),
                )
            else:
                selected = min(
                    priority_group,
                    key=lambda item: ((item.score or Decimal(0)), item.rule.name),
                )
            for item in evaluations:
                if item is selected:
                    continue
                if not item.triggered:
                    reason = item.trigger_reason
                elif item.rule.priority < selected.rule.priority:
                    reason = "lower_priority"
                elif item.direction == "down" and selected.direction == "up":
                    reason = "direction_tiebreak_scale_out_preferred"
                elif item.direction == selected.direction == "up":
                    reason = "lower_candidate_score"
                elif item.direction == selected.direction == "down":
                    reason = "higher_candidate_score"
                else:
                    reason = "rule_order_tiebreak"
                rejected.append(
                    {
                        "rule": item.rule.name,
                        "first_rejection_reason": reason,
                        "priority": item.rule.priority,
                        "direction": item.direction,
                        "decision_score": (
                            decimal_text(item.score) if item.score is not None else None
                        ),
                        "candidate_target_replicas": item.target_replicas,
                        "triggered": item.triggered,
                    }
                )
            rejected.sort(key=lambda item: (-item["priority"], item["rule"]))
            target = selected.target_replicas
            assert target is not None
            selected_name = selected.rule.name
            selected_direction = selected.direction
            selected_priority = selected.rule.priority
            selected_version = selected.rule.version
            basis = {
                "selected_rule": selected.rule.name,
                "rule_version": selected.rule.version,
                "metric": selected.metric.name,
                "aggregate": decimal_text(selected.aggregate or Decimal(0)),
                "target_value": decimal_text(selected.metric.target_value),
                "alert_threshold": decimal_text(selected.metric.alert_threshold),
                "decision_score": decimal_text(selected.score or Decimal(0)),
                "candidate_fixed": selected.candidate_fixed,
                "error_bound": decimal_text(selected.error_bound),
                "rounding_order": "ceil_or_floor_then_clamp",
                "window_start_ms": (
                    latest[selected.metric.name].window_start_ms
                    if selected.metric.name in latest
                    else None
                ),
                "window_end_ms": (
                    latest[selected.metric.name].window_end_ms
                    if selected.metric.name in latest
                    else None
                ),
            }
        else:
            target = current
            selected_name = None
            selected_direction = None
            selected_priority = None
            selected_version = 0
            for item in evaluations:
                rejected.append(
                    {
                        "rule": item.rule.name,
                        "first_rejection_reason": item.trigger_reason,
                        "priority": item.rule.priority,
                        "direction": item.direction,
                        "decision_score": None,
                        "candidate_target_replicas": None,
                        "triggered": False,
                    }
                )
            rejected.sort(key=lambda item: (-item["priority"], item["rule"]))
            basis = {
                "reason": "no_triggered_rule",
                "rounding_order": "ceil_or_floor_then_clamp",
            }

        workload_flags: list[PrecisionFlag] = []
        # Same-priority, same-direction rules whose score gap is below the
        # tolerance make the winner depend on numerical detail.
        for index, left in enumerate(triggered):
            for right in triggered[index + 1 :]:
                if left.rule.priority != right.rule.priority:
                    continue
                if left.direction != right.direction:
                    continue
                if left.score is None or right.score is None:
                    continue
                gap = abs(left.score - right.score)
                target_gap = abs(
                    Decimal(int(left.target_replicas or 0))
                    - Decimal(int(right.target_replicas or 0))
                )
                if gap > tolerance and target_gap > tolerance:
                    continue
                high_left = left.high_scale_candidate
                high_right = right.high_scale_candidate
                if high_left is None or high_right is None:
                    would_flip = False
                elif left.direction == "up":
                    would_flip = (high_left > high_right) != (
                        (left.score or Decimal(0)) > (right.score or Decimal(0))
                    )
                else:
                    would_flip = (high_left < high_right) != (
                        (left.score or Decimal(0)) < (right.score or Decimal(0))
                    )
                workload_flags.append(
                    PrecisionFlag(
                        workload=workload_name,
                        flag_type="close_candidate_scores",
                        rules=[left.rule.name, right.rule.name],
                        metrics=sorted({left.metric.name, right.metric.name}),
                        message=(
                            "candidate scores or target replica counts differ by "
                            "less than the declared tolerance"
                        ),
                        score_gap=gap,
                        tolerance=tolerance,
                        error_bound=left.error_bound + right.error_bound,
                        higher_scale=max(left.metric.scale, right.metric.scale) * 10,
                        would_flip_at_higher_scale=bool(would_flip),
                        steps=sorted(
                            {
                                fixed_inputs[left.metric.name].aggregate_step_id,
                                fixed_inputs[right.metric.name].aggregate_step_id,
                            }
                        ),
                    )
                )
            if left.fixed_divergence is not None:
                workload_flags.append(
                    PrecisionFlag(
                        workload=workload_name,
                        flag_type="fixed_decimal_divergence",
                        rules=[left.rule.name],
                        metrics=[left.metric.name],
                        message=left.fixed_divergence.message,
                        tolerance=left.fixed_divergence.tolerance,
                        error_bound=left.error_bound,
                        higher_scale=left.metric.scale * 10,
                        would_flip_at_higher_scale=(
                            left.high_scale_candidate is not None
                            and left.high_scale_candidate != left.target_replicas
                        ),
                        steps=[left.fixed_divergence.step_id],
                    )
                )
        for metric_name in sorted(
            {item.metric.name for item in triggered}
        ):
            metric_ledger = book.ledger(metric_name, bundle.metrics[metric_name].scale)
            if metric_ledger.cumulative_error_bound > bundle.metrics[metric_name].error_budget:
                workload_flags.append(
                    PrecisionFlag(
                        workload=workload_name,
                        flag_type="metric_error_budget_exceeded",
                        rules=[item.rule.name for item in triggered if item.metric.name == metric_name],
                        metrics=[metric_name],
                        message=(
                            "cumulative fixed-point conversion error exceeds the metric budget"
                        ),
                        error_bound=metric_ledger.cumulative_error_bound,
                        steps=[f"{metric_name}:window_aggregate"],
                    )
                )
            if metric_ledger.cumulative_error_bound > bundle.budget.error_upper_bound:
                workload_flags.append(
                    PrecisionFlag(
                        workload=workload_name,
                        flag_type="global_error_budget_exceeded",
                        rules=[item.rule.name for item in triggered if item.metric.name == metric_name],
                        metrics=[metric_name],
                        message="cumulative error exceeds the global error bound",
                        error_bound=metric_ledger.cumulative_error_bound,
                        steps=[f"{metric_name}:window_aggregate"],
                    )
                )
        flags.extend(workload_flags)
        decisions[workload_name] = WorkloadDecision(
            workload=workload_name,
            current_replicas=current,
            target_replicas=target,
            selected_rule=selected_name,
            selected_direction=selected_direction,
            selected_priority=selected_priority,
            selected_version=selected_version,
            delta=target - current,
            evaluations=evaluations,
            rejected_rules=rejected,
            precision_sensitive=bool(workload_flags),
            basis=basis,
            cache_key=(
                f"{workload_name}:{at_ms}:{selected_name or 'none'}:"
                f"{selected_version}:{target}"
            ),
        )

    first_divergence = min(
        book.divergences,
        key=lambda item: (item.step_no, item.metric, item.step_id),
        default=None,
    )
    return DecisionOutput(
        at_ms=at_ms,
        workloads=decisions,
        precision_flags=flags,
        first_divergence=first_divergence,
        windows=list(windows),
        ledger=book,
    )
