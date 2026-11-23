"""Deterministic budget-constrained action planning.

Logical time never depends on wall-clock duration.  Actions selected at a
round start all share the same logical start instant; an action is executable
only when its complete duration fits the round budget.  Once an action starts
it is never timed out: execution waits for completion and only a declared
success/failure outcome can transition it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .decision import WorkloadDecision
from .models import PolicyBundle, ScaleRule
from .util import decimal_text, sha256_text, canonical_json


@dataclass
class ActionPlan:
    action_key: str
    workload: str
    round_id: str
    rule_name: str
    rule_version: int
    direction: str
    delta: int
    current_replicas: int
    target_replicas: int
    priority: int
    duration_ms: int
    cpu_cost: Decimal
    memory_cost: Decimal
    status: str
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    outcome_success: bool = True
    outcome_reason: str | None = None
    start_at_ms: int = 0
    finish_at_ms: int = 0

    @property
    def will_start(self) -> bool:
        return self.status == "reserved"

    def request_payload(self) -> dict[str, Any]:
        return {
            "action_key": self.action_key,
            "workload": self.workload,
            "round_id": self.round_id,
            "rule_name": self.rule_name,
            "rule_version": self.rule_version,
            "direction": self.direction,
            "delta": self.delta,
            "current_replicas": self.current_replicas,
            "target_replicas": self.target_replicas,
            "duration_ms": self.duration_ms,
            "cpu_cost": decimal_text(self.cpu_cost),
            "memory_cost": decimal_text(self.memory_cost),
        }

    def fingerprint(self) -> str:
        return sha256_text(canonical_json(self.request_payload()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_key": self.action_key,
            "workload": self.workload,
            "round_id": self.round_id,
            "rule_name": self.rule_name,
            "rule_version": self.rule_version,
            "direction": self.direction,
            "delta": self.delta,
            "current_replicas": self.current_replicas,
            "target_replicas": self.target_replicas,
            "priority": self.priority,
            "duration_ms": self.duration_ms,
            "cpu_cost": decimal_text(self.cpu_cost),
            "memory_cost": decimal_text(self.memory_cost),
            "status": self.status,
            "reason": self.reason,
            "details": self.details,
            "outcome_success": self.outcome_success,
            "start_at_ms": self.start_at_ms,
            "finish_at_ms": self.finish_at_ms,
        }


@dataclass
class PlanningResult:
    actions: list[ActionPlan]
    newly_reserved_cpu: Decimal
    newly_reserved_memory: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "newly_reserved_cpu_core_ms": decimal_text(self.newly_reserved_cpu),
            "newly_reserved_memory_mib": decimal_text(self.newly_reserved_memory),
        }


def action_cost(
    bundle: PolicyBundle,
    workload_name: str,
    rule: ScaleRule,
    delta: int,
) -> tuple[int, Decimal, Decimal]:
    if bundle.budget is None:
        raise ValueError("bundle must contain a budget")
    workload = bundle.workloads[workload_name]
    duration = rule.duration_ms or bundle.budget.action_duration_ms
    moved = abs(delta)
    cpu = (
        Decimal(moved)
        * Decimal(workload.cpu_millicores)
        / Decimal(1000)
        * Decimal(duration)
    )
    memory = Decimal(moved) * Decimal(workload.memory_mib)
    return duration, cpu, memory


def plan_actions(
    bundle: PolicyBundle,
    decisions: Iterable[WorkloadDecision],
    *,
    round_id: str,
    at_ms: int,
    used_cpu_core_ms: Decimal,
    used_memory_mib: Decimal,
    runtime: Mapping[str, Mapping[str, Any]],
    active_per_workload: Mapping[str, int] | None = None,
    outcomes: Mapping[str, Mapping[str, Any]] | None = None,
) -> PlanningResult:
    if bundle.budget is None:
        raise ValueError("bundle must contain a budget")
    budget = bundle.budget
    actions: list[ActionPlan] = []
    candidates: list[tuple[tuple[Any, ...], WorkloadDecision]] = []
    for decision in decisions:
        if decision.delta == 0 or decision.selected_rule is None:
            continue
        rule = bundle.rules[decision.selected_rule]
        candidates.append(
            (
                (
                    -rule.priority,
                    0 if decision.selected_direction == "up" else 1,
                    decision.workload,
                    rule.name,
                ),
                decision,
            )
        )
    candidates.sort(key=lambda item: item[0])

    remaining_cpu = budget.pool_cpu_core_ms - used_cpu_core_ms
    remaining_memory = budget.pool_memory_mib - used_memory_mib
    active = dict(active_per_workload or {})
    outcome_map = outcomes or {}
    budget_closed_reason: str | None = None
    for _, decision in candidates:
        assert decision.selected_rule is not None
        rule = bundle.rules[decision.selected_rule]
        workload = bundle.workloads[decision.workload]
        direction = decision.selected_direction or "up"
        action_key = (
            f"{decision.workload}|{round_id}|{rule.name}|{rule.version}|{direction}"
        )
        duration, cpu, memory = action_cost(
            bundle, decision.workload, rule, decision.delta
        )
        start_at = at_ms
        finish_at = at_ms + duration
        runtime_row = runtime.get(decision.workload, {})
        cooldown_until = int(runtime_row.get("cooldown_until_ms", 0))
        freeze_until = int(runtime_row.get("freeze_until_ms", 0))
        backoff_until = int(runtime_row.get("backoff_until_ms", 0))
        known_outcome = outcome_map.get(decision.workload, {})
        success = bool(known_outcome.get("success", True))
        plan = ActionPlan(
            action_key=action_key,
            workload=decision.workload,
            round_id=round_id,
            rule_name=rule.name,
            rule_version=rule.version,
            direction=direction,
            delta=decision.delta,
            current_replicas=decision.current_replicas,
            target_replicas=decision.target_replicas,
            priority=rule.priority,
            duration_ms=duration,
            cpu_cost=cpu,
            memory_cost=memory,
            status="not_started",
            outcome_success=success,
            outcome_reason=known_outcome.get("reason"),
            start_at_ms=start_at,
            finish_at_ms=finish_at,
        )
        if direction == "up" and at_ms < freeze_until:
            plan.reason = "freeze_window"
            plan.details = {"freeze_until_ms": freeze_until, "at_ms": at_ms}
        elif at_ms < cooldown_until:
            plan.reason = "cooldown_window"
            plan.details = {"cooldown_until_ms": cooldown_until, "at_ms": at_ms}
        elif at_ms < backoff_until:
            plan.reason = "failure_backoff"
            plan.details = {
                "backoff_until_ms": backoff_until,
                "consecutive_failures": int(runtime_row.get("consecutive_failures", 0)),
                "at_ms": at_ms,
            }
        elif active.get(decision.workload, 0) >= budget.per_workload_concurrency:
            plan.reason = "workload_concurrency_limit"
            plan.details = {
                "active": active.get(decision.workload, 0),
                "limit": budget.per_workload_concurrency,
            }
        elif budget_closed_reason is not None:
            plan.status = "abandoned"
            plan.reason = budget_closed_reason
            plan.details = {
                "abandoned_after_earlier_budget_exhaustion": True,
                "at_ms": at_ms,
            }
        elif duration > budget.round_time_budget_ms:
            plan.status = "abandoned"
            plan.reason = "round_time_budget_exhausted"
            budget_closed_reason = plan.reason
            plan.details = {
                "required_ms": duration,
                "remaining_ms": budget.round_time_budget_ms,
            }
        elif cpu > remaining_cpu:
            plan.status = "abandoned"
            plan.reason = "cpu_budget_exhausted"
            budget_closed_reason = plan.reason
            plan.details = {
                "required_cpu_core_ms": decimal_text(cpu),
                "remaining_cpu_core_ms": decimal_text(max(remaining_cpu, Decimal(0))),
            }
        elif memory > remaining_memory:
            plan.status = "abandoned"
            plan.reason = "memory_budget_exhausted"
            budget_closed_reason = plan.reason
            plan.details = {
                "required_memory_mib": decimal_text(memory),
                "remaining_memory_mib": decimal_text(max(remaining_memory, Decimal(0))),
            }
        else:
            plan.status = "reserved"
            remaining_cpu -= cpu
            remaining_memory -= memory
            active[decision.workload] = active.get(decision.workload, 0) + 1
        actions.append(plan)

    return PlanningResult(
        actions=actions,
        newly_reserved_cpu=budget.pool_cpu_core_ms - used_cpu_core_ms - remaining_cpu,
        newly_reserved_memory=budget.pool_memory_mib - used_memory_mib - remaining_memory,
    )


def next_runtime_after_success(
    bundle: PolicyBundle,
    *,
    at_ms: int,
    duration_ms: int,
    direction: str,
    rule: ScaleRule,
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    completion = at_ms + duration_ms
    updated = dict(runtime)
    updated["cooldown_until_ms"] = completion + rule.cooldown_ms
    updated["consecutive_failures"] = 0
    updated["backoff_until_ms"] = 0
    if direction == "down":
        updated["freeze_until_ms"] = completion + rule.freeze_ms
    updated["last_action_clock_ms"] = completion
    return updated


def next_runtime_after_failure(
    bundle: PolicyBundle,
    *,
    at_ms: int,
    duration_ms: int,
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    if bundle.budget is None:
        raise ValueError("bundle must contain a budget")
    completion = at_ms + duration_ms
    failures = int(runtime.get("consecutive_failures", 0)) + 1
    exponent = min(failures - 1, 30)
    backoff = bundle.budget.backoff_base_ms * (2**exponent)
    updated = dict(runtime)
    updated["consecutive_failures"] = failures
    updated["backoff_until_ms"] = completion + backoff
    updated["last_action_clock_ms"] = completion
    return updated
