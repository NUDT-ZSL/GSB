from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

from .join import log2_cost, shape_connected
from .models import JoinPredicate, ReoptConfig, SampleSpec, Shape
from .precision import ErrorTracker, product_with_bound, sum_with_bound


@dataclass(frozen=True)
class ShapeEstimate:
    shape: Shape
    cost: float
    error_bound: float
    final_rows: float
    error_chain: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape.to_dict(),
            "cost": self.cost,
            "error_bound": self.error_bound,
            "final_rows": self.final_rows,
            "error_chain": self.error_chain,
        }


@dataclass(frozen=True)
class ReoptDecision:
    triggered: bool
    outcome: str
    from_shape: Shape
    to_shape: Shape
    switch_cost: float
    current_estimate: ShapeEstimate
    chosen_estimate: ShapeEstimate
    precision_sensitive: bool
    error_code: str | None
    error_chain: dict[str, Any]
    rejected_candidates: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "outcome": self.outcome,
            "from_shape": self.from_shape.to_dict(),
            "to_shape": self.to_shape.to_dict(),
            "switch_cost": self.switch_cost,
            "current_estimate": self.current_estimate.to_dict(),
            "chosen_estimate": self.chosen_estimate.to_dict(),
            "precision_sensitive": self.precision_sensitive,
            "error_code": self.error_code,
            "error_chain": self.error_chain,
            "rejected_candidates": self.rejected_candidates,
        }


def _selectivity_for(
    predicate: JoinPredicate,
    edge_selectivities: dict[tuple[str, str, str, str], float],
    sample: SampleSpec,
) -> float:
    direct = predicate.edge_key
    reverse = (predicate.right_table, predicate.right_column, predicate.left_table, predicate.left_column)
    if direct in edge_selectivities:
        return edge_selectivities[direct]
    if reverse in edge_selectivities:
        return edge_selectivities[reverse]
    if direct in sample.statistics.join_selectivities:
        return sample.statistics.join_selectivities[direct]
    if reverse in sample.statistics.join_selectivities:
        return sample.statistics.join_selectivities[reverse]
    return 1.0


def _step_conditions(
    table: str,
    joined_tables: set[str],
    predicates: tuple[JoinPredicate, ...],
) -> list[JoinPredicate]:
    conditions: list[JoinPredicate] = []
    for predicate in predicates:
        if predicate.left_table == table and predicate.right_table in joined_tables:
            conditions.append(predicate)
        elif predicate.right_table == table and predicate.left_table in joined_tables:
            conditions.append(predicate)
    return conditions


def _algorithm_cost(algorithm: str, left_rows: float, right_rows: float, output_rows: float) -> float:
    if algorithm == "hash":
        return 0.8 * left_rows + 1.2 * right_rows + 0.4 * output_rows + 2.0
    if algorithm == "sort_merge":
        return (
            0.45 * log2_cost(max(0, int(left_rows)))
            + 0.45 * log2_cost(max(0, int(right_rows)))
            + 0.9 * (left_rows + right_rows)
            + 0.5 * output_rows
            + 1.0
        )
    if algorithm == "nested_loop":
        return left_rows * right_rows + 0.25 * output_rows + 1.0
    raise ValueError(f"unknown algorithm: {algorithm}")


def estimate_shape(
    shape: Shape,
    row_counts: dict[str, int],
    edge_selectivities: dict[tuple[str, str, str, str], float],
    sample: SampleSpec,
    batch_interval: tuple[int, int] | None,
) -> ShapeEstimate:
    tracker = ErrorTracker()
    first_table = shape.join_order[0]
    current_rows = float(row_counts.get(first_table, 0))
    steps: list[dict[str, Any]] = []
    total_cost = 0.0
    total_bound = 0.0
    for index, table in enumerate(shape.join_order[1:], start=1):
        conditions = _step_conditions(table, set(shape.join_order[:index]), sample.plan.joins)
        selectivities = [
            _selectivity_for(predicate, edge_selectivities, sample) for predicate in conditions
        ]
        combined_selectivity = product_with_bound(
            selectivities,
            f"step_{index}_combined_join_selectivity",
            (first_table, table),
            batch_interval,
            tracker,
        )
        right_rows = float(row_counts.get(table, 0))
        output_rows = product_with_bound(
            [current_rows, right_rows, combined_selectivity],
            f"step_{index}_estimated_output_rows",
            (first_table, table),
            batch_interval,
            tracker,
        )
        algorithm = shape.algorithms[index - 1]
        algorithm_cost = _algorithm_cost(algorithm, current_rows, right_rows, output_rows)
        tracker.add(
            f"step_{index}_{algorithm}_cost",
            algorithm_cost,
            math.ulp(algorithm_cost) + algorithm_cost * 4.0 * math.ulp(1.0),
            (first_table, table),
            batch_interval,
            left_rows=current_rows,
            right_rows=right_rows,
            output_rows=output_rows,
        )
        steps.append(
            {
                "step": index,
                "join_table": table,
                "algorithm": algorithm,
                "left_rows": current_rows,
                "right_rows": right_rows,
                "combined_join_selectivity": combined_selectivity,
                "output_rows": output_rows,
                "cost": algorithm_cost,
            }
        )
        total_cost, total_bound = (
            sum_with_bound(
                [total_cost, algorithm_cost],
                f"step_{index}_cumulative_cost",
                (first_table, table),
                batch_interval,
                tracker,
            ),
            math.fsum([total_bound, math.ulp(algorithm_cost)]),
        )
        current_rows = output_rows
    chain = tracker.to_chain()
    chain["steps_detail"] = steps
    return ShapeEstimate(
        shape=shape,
        cost=total_cost,
        error_bound=math.fsum([chain["accumulated_error_bound"], total_bound]),
        final_rows=current_rows,
        error_chain=chain,
    )


def switch_cost(from_shape: Shape, to_shape: Shape, committed_rows: int) -> float:
    if from_shape == to_shape:
        return 0.0
    positions = {name: index for index, name in enumerate(from_shape.join_order)}
    reorder_distance = sum(
        abs(positions.get(name, len(positions)) - index) for index, name in enumerate(to_shape.join_order)
    )
    algorithm_changes = sum(
        1 for left, right in zip(from_shape.algorithms, to_shape.algorithms, strict=False) if left != right
    )
    return 1.0 + 0.002 * committed_rows + 0.25 * reorder_distance + 0.5 * algorithm_changes


def enumerate_shapes(sample: SampleSpec) -> list[Shape]:
    names = [table.name for table in sample.tables]
    predicates = sample.plan.joins
    valid_orders = [
        order for order in itertools.permutations(names) if shape_connected(Shape(order, tuple()), predicates)
    ]
    algorithms = sample.config.algorithm_candidates
    result: list[Shape] = []
    for order in valid_orders:
        for algorithm_choice in itertools.product(algorithms, repeat=max(0, len(order) - 1)):
            shape = Shape(join_order=tuple(order), algorithms=tuple(algorithm_choice))
            if shape_connected(shape, predicates):
                result.append(shape)
    result.sort(key=lambda shape: shape.canonical())
    return result


def decide_reopt(
    sample: SampleSpec,
    current_shape: Shape,
    row_counts: dict[str, int],
    edge_selectivities: dict[tuple[str, str, str, str], float],
    committed_rows: int,
    batch_interval: tuple[int, int] | None,
    trigger_sources: list[dict[str, Any]],
    budget_override: float | None = None,
) -> ReoptDecision:
    config: ReoptConfig = sample.config
    budget = config.switch_cost_budget if budget_override is None else budget_override
    tolerance = config.float_tolerance
    shapes = enumerate_shapes(sample)
    if current_shape not in shapes:
        shapes.append(current_shape)
    estimates = [
        estimate_shape(shape, row_counts, edge_selectivities, sample, batch_interval) for shape in shapes
    ]
    estimates.sort(key=lambda estimate: (estimate.cost, estimate.shape.canonical()))
    current_estimate = next(estimate for estimate in estimates if estimate.shape == current_shape)

    alternatives = [estimate for estimate in estimates if estimate.shape != current_shape]
    feasible = [
        estimate
        for estimate in alternatives
        if switch_cost(current_shape, estimate.shape, committed_rows) <= budget + tolerance
    ]
    feasible.sort(key=lambda estimate: (estimate.cost, estimate.shape.canonical()))
    all_beneficial = [estimate for estimate in alternatives if estimate.cost < current_estimate.cost]
    beneficial = [estimate for estimate in feasible if estimate.cost < current_estimate.cost]

    precision_sensitive = len(estimates) > 1 and abs(estimates[0].cost - estimates[1].cost) <= tolerance
    precision_chain: dict[str, Any] = {
        "precision_sensitive": precision_sensitive,
        "float_tolerance": tolerance,
        "error_bound_budget": config.error_bound_budget,
        "batch_interval": list(batch_interval) if batch_interval else None,
        "participating_tables": [table.name for table in sample.tables],
        "top_candidates": [
            {
                "shape": estimate.shape.to_dict(),
                "estimated_cost": estimate.cost,
                "error_bound": estimate.error_bound,
            }
            for estimate in estimates[: min(4, len(estimates))]
        ],
        "steps": [
            "estimate_each_shape_with_observed_row_counts_and_join_selectivities",
            "sum_algorithm_step_costs_with_fsum_error_bound",
            "compare_top_two_estimated_costs_against_float_tolerance",
            "gate_switch_cost_by_configured_budget",
        ],
        "trigger_sources": trigger_sources,
    }
    if len(estimates) > 1:
        precision_chain["top_two_cost_difference"] = abs(estimates[0].cost - estimates[1].cost)
        precision_chain["top_two_error_bounds"] = [
            estimates[0].error_bound,
            estimates[1].error_bound,
        ]
        precision_chain["error_bound_total"] = math.fsum(
            [estimates[0].error_bound, estimates[1].error_bound]
        )
        precision_chain["top_two_candidate_error_chains"] = [
            estimates[0].error_chain,
            estimates[1].error_chain,
        ]
    else:
        precision_chain["top_two_cost_difference"] = 0.0
        precision_chain["top_two_candidate_error_chains"] = []

    rejected: list[dict[str, Any]] = []
    for estimate in feasible:
        switch = switch_cost(current_shape, estimate.shape, committed_rows)
        rejected.append(
            {
                "shape": estimate.shape.to_dict(),
                "estimated_cost": estimate.cost,
                "switch_cost": switch,
                "reason": "lower_cost" if estimate.cost < current_estimate.cost else "no_cost_reduction",
            }
        )
    for estimate in alternatives:
        if estimate in feasible:
            continue
        rejected.append(
            {
                "shape": estimate.shape.to_dict(),
                "estimated_cost": estimate.cost,
                "switch_cost": switch_cost(current_shape, estimate.shape, committed_rows),
                "reason": "budget_exceeded",
            }
        )

    if all_beneficial and not beneficial:
        chosen = min(
            all_beneficial,
            key=lambda estimate: (
                switch_cost(current_shape, estimate.shape, committed_rows),
                estimate.cost,
                estimate.shape.canonical(),
            ),
        )
        return ReoptDecision(
            triggered=True,
            outcome="kept_original_budget",
            from_shape=current_shape,
            to_shape=current_shape,
            switch_cost=0.0,
            current_estimate=current_estimate,
            chosen_estimate=chosen,
            precision_sensitive=precision_sensitive,
            error_code="E_SWITCH_BUDGET",
            error_chain=precision_chain,
            rejected_candidates=[
                {
                    "shape": estimate.shape.to_dict(),
                    "estimated_cost": estimate.cost,
                    "switch_cost": switch_cost(current_shape, estimate.shape, committed_rows),
                    "reason": (
                        "budget_exceeded"
                        if switch_cost(current_shape, estimate.shape, committed_rows) > budget + tolerance
                        else "not_selected"
                    ),
                }
                for estimate in alternatives
            ],
        )

    if not beneficial:
        return ReoptDecision(
            triggered=True,
            outcome="keep_no_better_shape",
            from_shape=current_shape,
            to_shape=current_shape,
            switch_cost=0.0,
            current_estimate=current_estimate,
            chosen_estimate=current_estimate,
            precision_sensitive=precision_sensitive,
            error_code="E_NO_BETTER_SHAPE",
            error_chain=precision_chain,
            rejected_candidates=rejected,
        )

    chosen = beneficial[0]
    chosen_switch_cost = switch_cost(current_shape, chosen.shape, committed_rows)
    return ReoptDecision(
        triggered=True,
        outcome="switched",
        from_shape=current_shape,
        to_shape=chosen.shape,
        switch_cost=chosen_switch_cost,
        current_estimate=current_estimate,
        chosen_estimate=chosen,
        precision_sensitive=precision_sensitive,
        error_code=None,
        error_chain=precision_chain,
        rejected_candidates=rejected,
    )
