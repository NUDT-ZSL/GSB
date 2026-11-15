from __future__ import annotations

import heapq
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from .model import Point, Scenario, SearchParams, Terrain, UnitSpec
from .precision import SQRT2, CostLedger, build_cost_ledger
from .smoothing import SmoothedPath, smooth_path, validate_path

NEIGHBOR_OFFSETS = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)


class SearchCancelled(Exception):
    pass


@dataclass(frozen=True)
class SearchStats:
    reached: bool
    cost_fixed: int
    expanded_count: int
    generated_count: int
    heuristic_weight: Decimal
    heuristically_admissible: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "reached": self.reached,
            "cost_fixed": self.cost_fixed,
            "expanded_count": self.expanded_count,
            "generated_count": self.generated_count,
            "heuristic_weight": float(self.heuristic_weight),
            "heuristically_admissible": self.heuristically_admissible,
        }


@dataclass(frozen=True)
class SearchOutcome:
    path: tuple[Point, ...]
    stats: SearchStats


@dataclass(frozen=True)
class BudgetDecision:
    reachable: bool
    in_boundary_zone: bool
    policy: str
    budget_fixed: int
    budget: Decimal
    tolerance_fixed: int
    tolerance: Decimal
    difference_fixed: int
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "reachable": self.reachable,
            "in_boundary_zone": self.in_boundary_zone,
            "policy": self.policy,
            "budget": float(self.budget),
            "budget_fixed": self.budget_fixed,
            "tolerance": float(self.tolerance),
            "tolerance_fixed": self.tolerance_fixed,
            "difference_fixed": self.difference_fixed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PlanResult:
    unit_id: str | None
    start: Point
    goal: Point
    path: tuple[Point, ...]
    raw_path: tuple[Point, ...]
    weighted_candidate_path: tuple[Point, ...]
    cost: CostLedger
    budget: BudgetDecision
    weighted_search: SearchStats
    optimal_search: SearchStats
    smoothing: SmoothedPath
    possibly_non_optimal: bool
    actual_cost_ratio: float | None
    selected_search: str

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "start": list(self.start),
            "goal": list(self.goal),
            "path": [list(point) for point in self.path],
            "expanded_path": [list(step.cell) for step in self.cost.steps],
            "raw_path": [list(point) for point in self.raw_path],
            "weighted_candidate_path": [
                list(point) for point in self.weighted_candidate_path
            ],
            "path_cost_fixed": self.cost.fixed_total,
            "path_cost": float(self.cost.fixed_total) / float(self.cost.scale),
            "reference_cost": self.cost.reference_total,
            "cost_delta": self.cost.delta,
            "error_bound": self.cost.error_bound,
            "cost_within_tolerance": self.cost.within_tolerance,
            "budget_decision": self.budget.to_dict(),
            "weighted_search": self.weighted_search.to_dict(),
            "optimal_search": self.optimal_search.to_dict(),
            "smoothing": self.smoothing.to_dict(),
            "possibly_non_optimal": self.possibly_non_optimal,
            "actual_cost_ratio": self.actual_cost_ratio,
            "selected_search": self.selected_search,
            "ledger": self.cost.to_dict(),
        }


class Planner:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self._search_cache: dict[tuple[Point, Point, Decimal], SearchOutcome] = {}
        self._min_step_cost = self._compute_min_step_cost()

    def _compute_min_step_cost(self) -> int:
        terrain = self.scenario.terrain
        values: list[int] = []
        for row in range(terrain.height):
            for col in range(terrain.width):
                if not terrain.passable((row, col)):
                    continue
                values.append(terrain.fixed_orthogonal[row][col])
                if self.scenario.search.diagonal_rule != "never":
                    values.append(terrain.fixed_diagonal[row][col])
        return min(values) if values else 0

    def _heuristic(self, point: Point, goal: Point, weight: Decimal) -> Decimal:
        dx = abs(point[1] - goal[1])
        dy = abs(point[0] - goal[0])
        metric = self.scenario.search.metric
        if metric == "manhattan":
            distance = Decimal(dx + dy)
        elif metric == "chebyshev":
            distance = Decimal(max(dx, dy))
        else:
            distance = Decimal(max(dx, dy)) + (SQRT2 - 1) * Decimal(min(dx, dy))
        return weight * Decimal(self._min_step_cost) * distance

    def _can_step(self, source: Point, target: Point) -> bool:
        terrain = self.scenario.terrain
        if not terrain.passable(target):
            return False
        dr = target[0] - source[0]
        dc = target[1] - source[1]
        if max(abs(dr), abs(dc)) != 1:
            return False
        if dr != 0 and dc != 0:
            rule = self.scenario.search.diagonal_rule
            if rule == "never":
                return False
            if rule == "no_corner_cutting":
                if not terrain.passable((source[0] + dr, source[1])):
                    return False
                if not terrain.passable((source[0], source[1] + dc)):
                    return False
        return True

    def _astar(
        self,
        start: Point,
        goal: Point,
        weight: Decimal,
        cancelled: Callable[[], bool] | None = None,
    ) -> SearchOutcome:
        terrain: Terrain = self.scenario.terrain
        if start == goal:
            stats = SearchStats(
                reached=True,
                cost_fixed=0,
                expanded_count=0,
                generated_count=0,
                heuristic_weight=weight,
                heuristically_admissible=weight <= 1,
            )
            return SearchOutcome((start,), stats)
        if not terrain.passable(start) or not terrain.passable(goal):
            stats = SearchStats(
                reached=False,
                cost_fixed=0,
                expanded_count=0,
                generated_count=0,
                heuristic_weight=weight,
                heuristically_admissible=weight <= 1,
            )
            return SearchOutcome((), stats)

        start_h = self._heuristic(start, goal, weight)
        queue: list[tuple[Decimal, Decimal, int, int, int]] = [
            (start_h, start_h, 0, start[0], start[1])
        ]
        g_score: dict[Point, int] = {start: 0}
        parent: dict[Point, Point] = {}
        closed: set[Point] = set()
        expanded = 0
        generated = 0

        while queue:
            _, _, current_g, row, col = heapq.heappop(queue)
            current = (row, col)
            if current in closed:
                continue
            if current_g != g_score.get(current):
                continue
            closed.add(current)
            expanded += 1
            if cancelled is not None and expanded % 64 == 0 and cancelled():
                raise SearchCancelled("preflight cancelled")
            if current == goal:
                path: list[Point] = [current]
                while path[-1] != start:
                    path.append(parent[path[-1]])
                path.reverse()
                stats = SearchStats(
                    reached=True,
                    cost_fixed=current_g,
                    expanded_count=expanded,
                    generated_count=generated,
                    heuristic_weight=weight,
                    heuristically_admissible=weight <= 1,
                )
                return SearchOutcome(tuple(path), stats)

            for dr, dc in NEIGHBOR_OFFSETS:
                neighbor = (row + dr, col + dc)
                if neighbor in closed or not self._can_step(current, neighbor):
                    continue
                generated += 1
                tentative = current_g + terrain.fixed_step(current, neighbor)
                if tentative >= g_score.get(neighbor, 1 << 62):
                    continue
                g_score[neighbor] = tentative
                parent[neighbor] = current
                h_value = self._heuristic(neighbor, goal, weight)
                f_value = Decimal(tentative) + h_value
                heapq.heappush(
                    queue,
                    (f_value, h_value, tentative, neighbor[0], neighbor[1]),
                )

        stats = SearchStats(
            reached=False,
            cost_fixed=0,
            expanded_count=expanded,
            generated_count=generated,
            heuristic_weight=weight,
            heuristically_admissible=weight <= 1,
        )
        return SearchOutcome((), stats)

    def search(
        self,
        start: Point,
        goal: Point,
        weight: Decimal,
        cancelled: Callable[[], bool] | None = None,
    ) -> SearchOutcome:
        key = (start, goal, weight)
        if cancelled is None and key in self._search_cache:
            return self._search_cache[key]
        result = self._astar(start, goal, weight, cancelled)
        if cancelled is None:
            self._search_cache[key] = result
        return result

    def _budget_decision(self, cost_fixed: int, budget_fixed: int) -> BudgetDecision:
        precision = self.scenario.precision
        difference = cost_fixed - budget_fixed
        in_boundary = abs(difference) <= precision.tolerance_fixed
        if in_boundary:
            reachable = precision.budget_policy == "reachable"
            reason = (
                f"|cost-budget|={abs(difference)} fixed units <= "
                f"tolerance {precision.tolerance_fixed}; policy={precision.budget_policy}"
            )
        elif difference < 0:
            reachable = True
            reason = (
                f"cost is below budget by {abs(difference)} fixed units, "
                f"outside tolerance {precision.tolerance_fixed}"
            )
        else:
            reachable = False
            reason = (
                f"cost is above budget by {difference} fixed units, "
                f"outside tolerance {precision.tolerance_fixed}"
            )
        return BudgetDecision(
            reachable=reachable,
            in_boundary_zone=in_boundary,
            policy=precision.budget_policy,
            budget_fixed=budget_fixed,
            budget=Decimal(budget_fixed) / precision.scale,
            tolerance_fixed=precision.tolerance_fixed,
            tolerance=precision.tolerance,
            difference_fixed=difference,
            reason=reason,
        )

    def plan(
        self,
        start: Point,
        goal: Point,
        budget_fixed: int,
        *,
        unit_id: str | None = None,
        weight: Decimal | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> PlanResult:
        search = self.scenario.search
        requested_weight = search.weight if weight is None else weight
        weighted = self.search(start, goal, requested_weight, cancelled)
        baseline_weight = Decimal(1)
        if requested_weight == baseline_weight:
            optimal = weighted
        else:
            optimal = self.search(start, goal, baseline_weight, cancelled)

        selected_raw = optimal.path if optimal.path else weighted.path
        smoothing = smooth_path(
            self.scenario.terrain,
            selected_raw,
            self.scenario.smoothing,
            search.diagonal_rule,
        )
        if smoothing.expanded and not validate_path(
            self.scenario.terrain,
            smoothing.expanded,
            search.diagonal_rule,
        ):
            raise RuntimeError("smoothed path failed deterministic validation")
        ledger = build_cost_ledger(
            self.scenario.terrain,
            smoothing.expanded,
            self.scenario,
        )
        budget_decision = self._budget_decision(ledger.fixed_total, budget_fixed)
        if not smoothing.expanded:
            budget_decision = BudgetDecision(
                reachable=False,
                in_boundary_zone=False,
                policy=self.scenario.precision.budget_policy,
                budget_fixed=budget_fixed,
                budget=Decimal(budget_fixed) / self.scenario.precision.scale,
                tolerance_fixed=self.scenario.precision.tolerance_fixed,
                tolerance=self.scenario.precision.tolerance,
                difference_fixed=ledger.fixed_total - budget_fixed,
                reason="no passable path exists",
            )

        candidate_cost = weighted.stats.cost_fixed
        if weighted.path:
            candidate_smoothing = smooth_path(
                self.scenario.terrain,
                weighted.path,
                self.scenario.smoothing,
                search.diagonal_rule,
            )
            candidate_ledger = build_cost_ledger(
                self.scenario.terrain,
                candidate_smoothing.expanded,
                self.scenario,
            )
            candidate_cost = candidate_ledger.fixed_total
        optimal_cost = ledger.fixed_total
        ratio: float | None = None
        if optimal.stats.reached and optimal_cost > 0:
            ratio = candidate_cost / optimal_cost
        possibly_non_optimal = requested_weight > 1
        selected_search = (
            "weighted_search"
            if requested_weight == baseline_weight
            else "canonical_weight_1_guard"
        )
        return PlanResult(
            unit_id=unit_id,
            start=start,
            goal=goal,
            path=smoothing.anchors,
            raw_path=selected_raw,
            weighted_candidate_path=weighted.path,
            cost=ledger,
            budget=budget_decision,
            weighted_search=weighted.stats,
            optimal_search=optimal.stats,
            smoothing=smoothing,
            possibly_non_optimal=possibly_non_optimal,
            actual_cost_ratio=ratio,
            selected_search=selected_search,
        )

    def plan_unit(self, unit: UnitSpec, goal: Point | None = None) -> PlanResult:
        target = unit.goal if goal is None else goal
        return self.plan(
            unit.start,
            target,
            unit.budget_fixed,
            unit_id=unit.id,
        )
