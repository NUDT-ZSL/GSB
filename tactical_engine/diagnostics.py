from __future__ import annotations

from .planner import PlanResult


def diagnose_plan(plan: PlanResult, threshold: float) -> dict[str, object]:
    if threshold < 0:
        raise ValueError("diagnostic threshold must be non-negative")
    first_exceed = None
    maximum = None
    for step in plan.cost.steps:
        magnitude = abs(step.delta)
        if maximum is None or magnitude > maximum["magnitude"]:
            maximum = {
                "index": step.index,
                "cell": list(step.cell),
                "magnitude": magnitude,
                "delta": step.delta,
                "error_bound": step.error_bound,
            }
        if first_exceed is None and magnitude > threshold:
            first_exceed = {
                "index": step.index,
                "cell": list(step.cell),
                "delta": step.delta,
                "error_bound": step.error_bound,
                "fixed_cumulative": step.fixed_cumulative,
                "reference_cumulative": step.reference_cumulative,
            }
    return {
        "threshold": threshold,
        "first_exceed": first_exceed,
        "maximum_error": maximum,
        "final_error_bound": plan.cost.error_bound,
        "within_tolerance": plan.cost.within_tolerance,
        "budget_decision": plan.budget.to_dict(),
        "step_count": max(0, len(plan.cost.steps) - 1),
    }
