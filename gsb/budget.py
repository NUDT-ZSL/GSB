from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import EngineConfig


EXACT_MODE = "exact_stream"
ACCELERATED_MODE = "hot_split_two_phase"


def batch_cost_ms(config: EngineConfig, row_count: int, mode: str) -> float:
    per_k = row_count / 1000.0
    if mode == EXACT_MODE:
        return config.logical_clock_step_ms + per_k * config.exact_cost_ms_per_1k_rows
    return config.logical_clock_step_ms + per_k * config.accelerated_cost_ms_per_1k_rows


@dataclass(frozen=True)
class BudgetDecision:
    mode: str
    previous_mode: str
    elapsed_before_ms: float
    elapsed_after_ms: float
    projected_exact_ms: float
    projected_accelerated_ms: float
    budget_ms: float
    switched: bool
    reason: str
    rejected_modes: dict[str, str]
    budget_missed_projection: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "previous_mode": self.previous_mode,
            "elapsed_before_ms": self.elapsed_before_ms,
            "elapsed_after_ms": self.elapsed_after_ms,
            "projected_exact_ms": self.projected_exact_ms,
            "projected_accelerated_ms": self.projected_accelerated_ms,
            "budget_ms": self.budget_ms,
            "switched": self.switched,
            "reason": self.reason,
            "rejected_modes": self.rejected_modes,
            "budget_missed_projection": self.budget_missed_projection,
        }


def decide_mode(
    config: EngineConfig,
    current_mode: str,
    elapsed_ms: float,
    ordinal: int,
    rows_by_ordinal: dict[int, int],
    switch_overhead_used: bool,
) -> BudgetDecision:
    remaining_ordinals = sorted(value for value in rows_by_ordinal if value >= ordinal)
    remaining_rows = [rows_by_ordinal[value] for value in remaining_ordinals]
    projected_exact = elapsed_ms + sum(
        batch_cost_ms(config, rows, EXACT_MODE) for rows in remaining_rows
    )
    overhead = 0.0 if switch_overhead_used else config.switch_overhead_ms
    projected_accelerated = (
        elapsed_ms
        + overhead
        + sum(batch_cost_ms(config, rows, ACCELERATED_MODE) for rows in remaining_rows)
    )
    current_rows = rows_by_ordinal.get(ordinal, 0)
    budget = config.time_budget_ms
    if current_mode == ACCELERATED_MODE:
        elapsed_after = elapsed_ms + batch_cost_ms(
            config, current_rows, ACCELERATED_MODE
        )
        return BudgetDecision(
            mode=ACCELERATED_MODE,
            previous_mode=current_mode,
            elapsed_before_ms=elapsed_ms,
            elapsed_after_ms=elapsed_after,
            projected_exact_ms=projected_exact,
            projected_accelerated_ms=projected_accelerated,
            budget_ms=budget,
            switched=False,
            reason="accelerated mode is already active",
            rejected_modes={
                EXACT_MODE: "exact mode was rejected at the recorded switch point"
            },
            budget_missed_projection=elapsed_after > budget and (
                elapsed_after + 0.0 > budget
            ),
        )
    if projected_exact <= budget:
        elapsed_after = elapsed_ms + batch_cost_ms(config, current_rows, EXACT_MODE)
        return BudgetDecision(
            mode=EXACT_MODE,
            previous_mode=current_mode,
            elapsed_before_ms=elapsed_ms,
            elapsed_after_ms=elapsed_after,
            projected_exact_ms=projected_exact,
            projected_accelerated_ms=projected_accelerated,
            budget_ms=budget,
            switched=False,
            reason="projected exact latency remains within the logical budget",
            rejected_modes={
                ACCELERATED_MODE: "not required; exact truth path fits the budget"
            },
            budget_missed_projection=False,
        )
    missed = projected_accelerated > budget
    elapsed_after = (
        elapsed_ms + overhead + batch_cost_ms(config, current_rows, ACCELERATED_MODE)
    )
    reason = (
        f"projected exact latency {projected_exact:.9f} ms exceeds budget "
        f"{budget:.9f} ms; switched before batch ordinal {ordinal}"
    )
    if missed:
        reason += (
            f"; projected accelerated latency {projected_accelerated:.9f} ms also "
            "exceeds budget, accelerated fallback retained because it is the only "
            "budget-reducing path"
        )
    else:
        reason += (
            f"; projected accelerated latency {projected_accelerated:.9f} ms fits budget"
        )
    return BudgetDecision(
        mode=ACCELERATED_MODE,
        previous_mode=current_mode,
        elapsed_before_ms=elapsed_ms,
        elapsed_after_ms=elapsed_after,
        projected_exact_ms=projected_exact,
        projected_accelerated_ms=projected_accelerated,
        budget_ms=budget,
        switched=True,
        reason=reason,
        rejected_modes={
            EXACT_MODE: (
                f"projected {projected_exact:.9f} ms > budget {budget:.9f} ms"
            )
        },
        budget_missed_projection=missed,
    )

