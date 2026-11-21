from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .aggregation import AggregateResult
from .fixed import SCALE, ceil_div, fixed_text, to_fixed
from .model import MetricSource, ResourceBudget, ScalingRule, Workload


@dataclass(frozen=True)
class RuleRuntime:
    cooldown_until_ms: int = 0
    backoff_until_ms: int = 0
    consecutive_failures: int = 0
    last_success_ms: int | None = None
    last_failure_ms: int | None = None


@dataclass(frozen=True)
class WorkloadDecision:
    workload_id: str
    workload_name: str
    from_replicas: int
    candidate_replicas: int
    target_replicas: int
    selected_rule_id: str | None
    selected_rule_version: int | None
    direction: str | None
    delta_replicas: int
    status: str
    reason: str
    logical_now_ms: int
    rule_evaluations: tuple[dict[str, Any], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "workloadId": self.workload_id,
            "workloadName": self.workload_name,
            "fromReplicas": self.from_replicas,
            "candidateReplicas": self.candidate_replicas,
            "targetReplicas": self.target_replicas,
            "selectedRuleId": self.selected_rule_id,
            "selectedRuleVersion": self.selected_rule_version,
            "direction": self.direction,
            "deltaReplicas": self.delta_replicas,
            "status": self.status,
            "reason": self.reason,
            "logicalNowMs": self.logical_now_ms,
            "ruleEvaluations": list(self.rule_evaluations),
        }


def _percent_delta(current_replicas: int, percent: Decimal) -> int:
    percent_fixed = to_fixed(percent, "percent")
    return ceil_div(current_replicas * percent_fixed, SCALE * 100)


def _candidate(
    *,
    rule: ScalingRule,
    workload: Workload,
    current_replicas: int,
    aggregate: AggregateResult,
) -> tuple[int, int, int, str]:
    step_delta = max(rule.step, _percent_delta(current_replicas, rule.percent))
    target_fixed = aggregate.target_fixed
    if target_fixed <= 0:
        return current_replicas, step_delta, 0, "target_not_positive"
    if rule.direction == "scale_out":
        ratio_candidate = ceil_div(
            current_replicas * aggregate.fixed_value,
            target_fixed,
        )
        raw_candidate = max(current_replicas + step_delta, ratio_candidate)
        clamped = min(max(raw_candidate, workload.min_replicas), workload.max_replicas)
        if clamped == current_replicas:
            reason = "clamped_at_max_replicas"
            if current_replicas >= workload.max_replicas:
                reason = "max_replicas_reached"
            return clamped, step_delta, ratio_candidate, reason
        return clamped, step_delta, ratio_candidate, "actionable"
    ratio_candidate = (
        current_replicas * aggregate.fixed_value // target_fixed
        if target_fixed > 0
        else 0
    )
    raw_candidate = min(current_replicas - step_delta, ratio_candidate)
    clamped = min(max(raw_candidate, workload.min_replicas), workload.max_replicas)
    if clamped == current_replicas:
        reason = "clamped_at_min_replicas"
        if current_replicas <= workload.min_replicas:
            reason = "min_replicas_reached"
        return clamped, step_delta, ratio_candidate, reason
    return clamped, step_delta, ratio_candidate, "actionable"


def decide_workload(
    workload: Workload,
    current_replicas: int,
    rules: list[ScalingRule],
    metrics: dict[str, MetricSource],
    aggregates: dict[str, AggregateResult],
    runtimes: dict[str, RuleRuntime],
    budget: ResourceBudget,
    logical_now_ms: int,
) -> WorkloadDecision:
    if not rules:
        return WorkloadDecision(
            workload_id=workload.id,
            workload_name=workload.name,
            from_replicas=current_replicas,
            candidate_replicas=current_replicas,
            target_replicas=current_replicas,
            selected_rule_id=None,
            selected_rule_version=None,
            direction=None,
            delta_replicas=0,
            status="no_action",
            reason="no_rules",
            logical_now_ms=logical_now_ms,
            rule_evaluations=(),
        )

    freeze = budget.is_frozen(logical_now_ms)
    evaluations: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for rule in sorted(rules, key=lambda item: (item.priority, item.id)):
        runtime = runtimes.get(rule.id, RuleRuntime())
        aggregate = aggregates.get(rule.metric_source_id)
        metric = metrics.get(rule.metric_source_id)
        base: dict[str, Any] = {
            "ruleId": rule.id,
            "ruleVersion": rule.version,
            "priority": rule.priority,
            "direction": rule.direction,
            "metricSourceId": rule.metric_source_id,
            "cooldownUntilMs": runtime.cooldown_until_ms,
            "backoffUntilMs": runtime.backoff_until_ms,
            "consecutiveFailures": runtime.consecutive_failures,
            "status": "rejected",
            "reason": None,
        }
        if metric is None or aggregate is None:
            base.update(
                {
                    "reason": "metric_unavailable",
                    "hit": False,
                    "candidateReplicas": current_replicas,
                }
            )
            evaluations.append(base)
            continue
        aggregate_fixed = aggregate.fixed_value
        target_fixed = aggregate.target_fixed
        alert_fixed = aggregate.alert_threshold_fixed
        if rule.direction == "scale_out":
            hit = aggregate_fixed >= alert_fixed and aggregate_fixed > target_fixed
        else:
            hit = aggregate_fixed <= alert_fixed and aggregate_fixed < target_fixed
        candidate, step_delta, ratio_candidate, candidate_reason = _candidate(
            rule=rule,
            workload=workload,
            current_replicas=current_replicas,
            aggregate=aggregate,
        )
        base.update(
            {
                "hit": hit,
                "aggregateValue": aggregate.fixed_text,
                "targetValue": fixed_text(target_fixed),
                "alertThreshold": fixed_text(alert_fixed),
                "stepDelta": step_delta,
                "ratioCandidateReplicas": ratio_candidate,
                "candidateReplicas": candidate,
            }
        )
        if runtime.cooldown_until_ms > logical_now_ms:
            base["reason"] = "cooldown_active"
        elif runtime.backoff_until_ms > logical_now_ms:
            base["reason"] = "backoff_active"
        elif not hit:
            base["reason"] = "threshold_not_met"
        elif rule.direction == "scale_out" and freeze is not None:
            base["reason"] = "freeze_window_blocks_scale_out"
            base["freezeWindowId"] = freeze.id
        elif candidate == current_replicas:
            base["reason"] = candidate_reason
        else:
            base["reason"] = "eligible"
            base["status"] = "eligible"
            eligible.append(base)
        evaluations.append(base)

    selected: dict[str, Any] | None = None
    for evaluation in eligible:
        if selected is None:
            selected = evaluation
            evaluation["status"] = "selected"
            evaluation["reason"] = "selected_by_priority"
            continue
        if evaluation["direction"] == selected["direction"]:
            evaluation["reason"] = "shadowed_by_higher_priority"
        else:
            evaluation["reason"] = "direction_conflict_with_higher_priority"
        evaluation["status"] = "rejected"
        evaluation["winnerRuleId"] = selected["ruleId"]

    if selected is None:
        return WorkloadDecision(
            workload_id=workload.id,
            workload_name=workload.name,
            from_replicas=current_replicas,
            candidate_replicas=current_replicas,
            target_replicas=current_replicas,
            selected_rule_id=None,
            selected_rule_version=None,
            direction=None,
            delta_replicas=0,
            status="no_action",
            reason="no_eligible_rule",
            logical_now_ms=logical_now_ms,
            rule_evaluations=tuple(evaluations),
        )

    target = int(selected["candidateReplicas"])
    return WorkloadDecision(
        workload_id=workload.id,
        workload_name=workload.name,
        from_replicas=current_replicas,
        candidate_replicas=target,
        target_replicas=target,
        selected_rule_id=selected["ruleId"],
        selected_rule_version=selected["ruleVersion"],
        direction=selected["direction"],
        delta_replicas=target - current_replicas,
        status="action",
        reason="selected_by_priority",
        logical_now_ms=logical_now_ms,
        rule_evaluations=tuple(evaluations),
    )

