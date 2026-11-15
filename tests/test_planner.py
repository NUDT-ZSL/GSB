from __future__ import annotations

import copy
import math
from decimal import Decimal

import pytest

from tactical_engine.planner import Planner
from tactical_engine.scenario import parse_scenario
from tactical_engine.smoothing import validate_path


def _plan(simple_raw, weight: str, budget: float | None = None):
    scenario = parse_scenario(copy.deepcopy(simple_raw))
    unit = scenario.units[0]
    planner = Planner(scenario)
    budget_fixed = (
        unit.budget_fixed
        if budget is None
        else int(round(budget * float(scenario.precision.scale)))
    )
    return planner.plan(
        unit.start,
        unit.goal,
        budget_fixed,
        unit_id=unit.id,
        weight=Decimal(weight),
    )


def test_fixed_cost_is_weight_invariant_and_repeatable(simple_raw):
    plan_one = _plan(simple_raw, "1")
    plan_zero = _plan(simple_raw, "0.5")
    plan_heavy = _plan(simple_raw, "3")
    repeat = _plan(simple_raw, "1")
    assert plan_one.cost.fixed_total == plan_zero.cost.fixed_total
    assert plan_one.cost.fixed_total == plan_heavy.cost.fixed_total
    assert plan_one.path == repeat.path
    assert plan_one.weighted_search.expanded_count == repeat.weighted_search.expanded_count
    assert plan_heavy.possibly_non_optimal is True
    assert plan_one.possibly_non_optimal is False
    assert plan_heavy.actual_cost_ratio is not None
    assert plan_heavy.actual_cost_ratio >= 1.0 - 1e-12


def test_ledger_tracks_every_cell_and_error_bound(simple_scenario):
    unit = simple_scenario.units[0]
    planner = Planner(simple_scenario)
    plan = planner.plan_unit(unit)
    terrain = simple_scenario.terrain
    cells = tuple(step.cell for step in plan.cost.steps)
    expected_fixed = sum(
        terrain.fixed_step(source, target)
        for source, target in zip(cells, cells[1:])
    )
    assert plan.cost.fixed_total == expected_fixed
    assert len(plan.cost.steps) == len(plan.smoothing.expanded)
    for step in plan.cost.steps:
        assert abs(step.delta) <= step.error_bound + 1e-15
    assert plan.cost.within_tolerance
    assert abs(plan.cost.delta) <= float(simple_scenario.precision.tolerance)
    assert plan.cost.error_bound >= abs(plan.cost.delta)


def test_smoothed_path_is_legal_and_recomputed(simple_raw):
    plan = _plan(simple_raw, "1")
    scenario = parse_scenario(simple_raw)
    expanded = tuple(step.cell for step in plan.cost.steps)
    assert validate_path(scenario.terrain, expanded, scenario.search.diagonal_rule)
    recalculated = sum(
        scenario.terrain.fixed_step(source, target)
        for source, target in zip(expanded, expanded[1:])
    )
    assert recalculated == plan.cost.fixed_total
    assert plan.smoothing.segments[-1] == (plan.smoothing.anchors[-2], plan.smoothing.anchors[-1])


@pytest.mark.parametrize(
    ("policy", "budget", "expected"),
    [
        ("reachable", 8.005, True),
        ("unreachable", 8.005, False),
        ("reachable", 8.02, True),
        ("reachable", 7.98, False),
        ("unreachable", 7.98, False),
    ],
)
def test_budget_boundary_uses_fixed_units(simple_raw, policy, budget, expected):
    simple_raw["precision"]["budget_policy"] = policy
    scenario = parse_scenario(copy.deepcopy(simple_raw))
    budget_fixed = int(round(budget * float(scenario.precision.scale)))
    unit = scenario.units[0]
    plan = Planner(scenario).plan(unit.start, unit.goal, budget_fixed, unit_id=unit.id)
    assert plan.cost.fixed_total == 8000
    assert plan.budget.difference_fixed == 8000 - budget_fixed
    assert plan.budget.reachable is expected
    assert plan.budget.in_boundary_zone is (abs(8000 - budget_fixed) <= 10)


def test_unreachable_target_is_not_reported_as_zero_cost_reachable(simple_raw):
    simple_raw["terrain"]["blocked"] = [
        [0, 1],
        [1, 0],
        [1, 2],
        [2, 1],
    ]
    simple_raw["units"][0]["goal"] = [2, 0]
    simple_raw["units"][0]["candidates"] = [{"id": "isolated", "cell": [2, 0]}]
    scenario = parse_scenario(simple_raw)
    unit = scenario.units[0]
    plan = Planner(scenario).plan_unit(unit)
    assert not plan.cost.steps
    assert plan.budget.reachable is False
