from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Iterable

import numpy as np

from .model import Point, Scenario, Terrain

ONE = Decimal(1)
HALF = Decimal("0.5")
U = 2.0 ** -53
SQRT2 = Decimal("1.414213562373095048801688724")


def quantize_fixed(value: Decimal, scale: Decimal) -> int:
    if not value.is_finite() or not scale.is_finite() or scale <= 0:
        raise ValueError("fixed-point quantization requires finite value and positive scale")
    return int((value * scale).quantize(ONE, rounding=ROUND_HALF_UP))


def tolerance_to_fixed(value: Decimal, scale: Decimal) -> int:
    return int((value * scale).to_integral_value(rounding=ROUND_CEILING))


def build_terrain(
    width: int,
    height: int,
    costs: tuple[tuple[Decimal, ...], ...],
    blocked: frozenset[Point],
    scale: Decimal,
    diagonal_cost_factor: Decimal,
) -> Terrain:
    fixed_orthogonal: list[tuple[int, ...]] = []
    fixed_diagonal: list[tuple[int, ...]] = []
    for row in costs:
        fixed_orthogonal.append(tuple(quantize_fixed(cost, scale) for cost in row))
        fixed_diagonal.append(
            tuple(quantize_fixed(cost * diagonal_cost_factor, scale) for cost in row)
        )
    return Terrain(
        width=width,
        height=height,
        costs=costs,
        blocked=blocked,
        fixed_orthogonal=tuple(fixed_orthogonal),
        fixed_diagonal=tuple(fixed_diagonal),
        diagonal_cost_factor=diagonal_cost_factor,
    )


@dataclass(frozen=True)
class CostStep:
    index: int
    cell: Point
    fixed_step: int
    fixed_cumulative: int
    reference_step: float
    reference_cumulative: float
    delta: float
    quantization_bound: float
    float_bound: float
    error_bound: float

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "cell": list(self.cell),
            "fixed_step": self.fixed_step,
            "fixed_cumulative": self.fixed_cumulative,
            "reference_step": self.reference_step,
            "reference_cumulative": self.reference_cumulative,
            "delta": self.delta,
            "quantization_bound": self.quantization_bound,
            "float_bound": self.float_bound,
            "error_bound": self.error_bound,
        }


@dataclass(frozen=True)
class CostLedger:
    steps: tuple[CostStep, ...]
    fixed_total: int
    reference_total: float
    delta: float
    quantization_bound: float
    float_bound: float
    error_bound: float
    scale: Decimal
    tolerance: Decimal
    within_tolerance: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "fixed_total": self.fixed_total,
            "fixed_total_cost": float(self.fixed_total) / float(self.scale),
            "reference_total": self.reference_total,
            "delta": self.delta,
            "quantization_bound": self.quantization_bound,
            "float_bound": self.float_bound,
            "error_bound": self.error_bound,
            "tolerance": float(self.tolerance),
            "within_tolerance": self.within_tolerance,
            "steps": [step.to_dict() for step in self.steps],
        }


def _float_accumulation_bound(step_count: int, terms: Iterable[float]) -> float:
    if step_count <= 0:
        return 0.0
    denominator = 1.0 - step_count * U
    if denominator <= 0:
        return math.inf
    gamma = (step_count * U) / denominator
    magnitude = math.fsum(abs(term) for term in terms)
    return gamma * magnitude


def build_cost_ledger(
    terrain: Terrain,
    path: tuple[Point, ...],
    scenario: Scenario,
) -> CostLedger:
    if not path:
        return CostLedger(
            (),
            0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            scenario.precision.scale,
            scenario.precision.tolerance,
            True,
        )

    fixed_cumulative = 0
    reference_cumulative = np.float64(0.0)
    steps: list[CostStep] = [
        CostStep(
            index=0,
            cell=path[0],
            fixed_step=0,
            fixed_cumulative=0,
            reference_step=0.0,
            reference_cumulative=0.0,
            delta=0.0,
            quantization_bound=0.0,
            float_bound=0.0,
            error_bound=0.0,
        )
    ]
    reference_terms: list[float] = []
    scale_float = float(scenario.precision.scale)
    half_quantum = 0.5 / scale_float

    for index, (source, target) in enumerate(zip(path, path[1:]), start=1):
        fixed_step = terrain.fixed_step(source, target)
        reference_step = np.float64(terrain.reference_step(source, target))
        fixed_cumulative += fixed_step
        reference_cumulative = np.float64(reference_cumulative + reference_step)
        reference_terms.append(float(reference_step))

        delta = float(fixed_cumulative) / scale_float - float(reference_cumulative)
        quantization_bound = math.nextafter(index * half_quantum, math.inf)
        float_bound = _float_accumulation_bound(index, reference_terms)
        error_bound = math.nextafter(quantization_bound + float_bound, math.inf)
        steps.append(
            CostStep(
                index=index,
                cell=target,
                fixed_step=fixed_step,
                fixed_cumulative=fixed_cumulative,
                reference_step=float(reference_step),
                reference_cumulative=float(reference_cumulative),
                delta=delta,
                quantization_bound=quantization_bound,
                float_bound=float_bound,
                error_bound=error_bound,
            )
        )

    final = steps[-1]
    delta = final.delta
    error_bound = final.error_bound
    within = abs(delta) <= float(scenario.precision.tolerance)
    return CostLedger(
        steps=tuple(steps),
        fixed_total=fixed_cumulative,
        reference_total=final.reference_cumulative,
        delta=delta,
        quantization_bound=final.quantization_bound,
        float_bound=final.float_bound,
        error_bound=error_bound,
        scale=scenario.precision.scale,
        tolerance=scenario.precision.tolerance,
        within_tolerance=within,
    )
