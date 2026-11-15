from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

Point = tuple[int, int]


@dataclass(frozen=True)
class Terrain:
    width: int
    height: int
    costs: tuple[tuple[Decimal, ...], ...]
    blocked: frozenset[Point]
    fixed_orthogonal: tuple[tuple[int, ...], ...]
    fixed_diagonal: tuple[tuple[int, ...], ...]
    diagonal_cost_factor: Decimal

    def in_bounds(self, point: Point) -> bool:
        row, col = point
        return 0 <= row < self.height and 0 <= col < self.width

    def passable(self, point: Point) -> bool:
        return self.in_bounds(point) and point not in self.blocked

    def fixed_step(self, source: Point, target: Point) -> int:
        diagonal = source[0] != target[0] and source[1] != target[1]
        table = self.fixed_diagonal if diagonal else self.fixed_orthogonal
        return table[target[0]][target[1]]

    def reference_step(self, source: Point, target: Point) -> float:
        diagonal = source[0] != target[0] and source[1] != target[1]
        factor = self.diagonal_cost_factor if diagonal else Decimal(1)
        return float(self.costs[target[0]][target[1]] * factor)


@dataclass(frozen=True)
class SearchParams:
    weight: Decimal
    metric: str
    diagonal_rule: str
    diagonal_cost_factor: Decimal


@dataclass(frozen=True)
class PrecisionSpec:
    scale: Decimal
    tolerance: Decimal
    tolerance_fixed: int
    budget_policy: str


@dataclass(frozen=True)
class SmoothingParams:
    enabled: bool
    algorithm: str
    max_lookahead: int


@dataclass(frozen=True)
class Candidate:
    id: str
    cell: Point


@dataclass(frozen=True)
class UnitSpec:
    id: str
    name: str
    start: Point
    goal: Point
    budget: Decimal
    budget_fixed: int
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class ReplayEvent:
    index: int
    kind: str
    unit_id: str
    data: dict[str, Any]


@dataclass(frozen=True)
class Scenario:
    schema_version: int
    scenario_id: str
    terrain: Terrain
    search: SearchParams
    precision: PrecisionSpec
    smoothing: SmoothingParams
    concurrency: int
    memory_budget_bytes: int
    units: tuple[UnitSpec, ...]
    replay: tuple[ReplayEvent, ...]
    fingerprint: str

    def unit(self, unit_id: str) -> UnitSpec:
        for unit in self.units:
            if unit.id == unit_id:
                return unit
        raise KeyError(unit_id)
