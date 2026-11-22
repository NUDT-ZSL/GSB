"""Fixed-point error accounting for observations and drift decisions."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from .join import StageObservation
from .models import Scenario


@dataclass(frozen=True)
class QuantizedRatio:
    exact_numerator: int
    exact_denominator: int
    scale: int
    fixed_numerator: int
    fixed_value: float
    rounding_direction: str
    absolute_error_bound: float
    relative_error_bound: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "exact_numerator": self.exact_numerator,
            "exact_denominator": self.exact_denominator,
            "scale": self.scale,
            "fixed_numerator": self.fixed_numerator,
            "fixed_value": self.fixed_value,
            "rounding_direction": self.rounding_direction,
            "absolute_error_bound": self.absolute_error_bound,
            "relative_error_bound": self.relative_error_bound,
        }


def quantize_fraction(numerator: int, denominator: int, scale: int) -> QuantizedRatio:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if scale <= 0:
        raise ValueError("scale must be positive")
    scaled_numerator = numerator * scale
    quotient, remainder = divmod(scaled_numerator, denominator)
    if remainder == 0:
        direction = "EXACT"
    elif remainder * 2 >= denominator:
        quotient += 1
        direction = "UP_HALF" if remainder * 2 == denominator else "UP"
    else:
        direction = "DOWN"
    exact = numerator / denominator
    fixed = quotient / scale
    abs_error = abs(quotient * denominator - numerator * scale) / (scale * denominator)
    relative = abs_error / max(abs(exact), 1.0 / scale)
    return QuantizedRatio(
        exact_numerator=numerator,
        exact_denominator=denominator,
        scale=scale,
        fixed_numerator=quotient,
        fixed_value=fixed,
        rounding_direction=direction,
        absolute_error_bound=abs_error,
        relative_error_bound=relative,
    )


def quantize_decimal(value: float, scale: int) -> QuantizedRatio:
    with localcontext() as ctx:
        ctx.prec = 50
        decimal_value = Decimal(str(value))
        scaled = decimal_value * scale
        rounded = scaled.to_integral_value(rounding=ROUND_HALF_UP)
        difference = scaled - rounded
        if difference == 0:
            direction = "EXACT"
        elif difference > 0:
            direction = "DOWN"
        else:
            direction = "UP"
        q_int = int(rounded)
        q_value = Decimal(q_int) / Decimal(scale)
        abs_error = abs(q_value - decimal_value)
        relative = abs_error / max(abs(decimal_value), Decimal(1) / Decimal(scale))
    return QuantizedRatio(
        exact_numerator=int(decimal_value * 10**12),
        exact_denominator=10**12,
        scale=scale,
        fixed_numerator=q_int,
        fixed_value=float(q_value),
        rounding_direction=direction,
        absolute_error_bound=float(abs_error),
        relative_error_bound=float(relative),
    )


def observation_ledger(stage: StageObservation, scale: int) -> dict[str, Any]:
    quantized = quantize_fraction(
        stage.selectivity_numerator,
        stage.selectivity_denominator,
        scale,
    )
    return {
        "kind": "observation_point",
        "join_id": stage.join_id,
        "batch_step": stage.step,
        "source_table": stage.source_table,
        "source_key": stage.source_key,
        "participating_tables": sorted(
            {stage.source_table, stage.added_table}
        ),
        "scale": scale,
        "rounding_direction": quantized.rounding_direction,
        "quantization": quantized.to_dict(),
        "floating_accumulation_bound": 8.0
        * max(math.ulp(quantized.fixed_value), 1.0 / scale),
        "formula": "fixed_scale(output_rows) / fixed_scale(left_rows * right_rows)",
    }


def _fixed_error_interval(quantized: QuantizedRatio) -> tuple[float, float]:
    center = quantized.fixed_value
    bound = quantized.absolute_error_bound
    low = max(0.0, center - bound)
    high = min(1.0, center + bound)
    return low, high


def drift_ledger(
    *,
    actual_numerator: int,
    actual_denominator: int,
    estimate: float,
    scale: int,
    batch_interval: Sequence[str],
    observation_points: Sequence[Mapping[str, Any]],
    participating_tables: Sequence[str],
    source_table: str,
    source_key: str,
    join_id: str,
) -> tuple[float, dict[str, Any]]:
    actual = quantize_fraction(actual_numerator, actual_denominator, scale)
    estimated = quantize_decimal(estimate, scale)
    actual_low, actual_high = _fixed_error_interval(actual)
    est_low, est_high = _fixed_error_interval(estimated)
    central = abs(actual.fixed_value - estimated.fixed_value) / max(estimated.fixed_value, 1.0 / scale)
    ratios = []
    for a in (actual_low, actual_high):
        for e in (est_low, est_high):
            ratios.append(abs(a - e) / max(e, 1.0 / scale))
    ratio_low = min(ratios) if ratios else 0.0
    ratio_high = max(ratios) if ratios else 0.0
    ratio_error_bound = max(abs(ratio_high - central), abs(central - ratio_low))
    operation_ulp = 8.0 * max(
        math.ulp(central),
        math.ulp(actual.fixed_value),
        math.ulp(estimated.fixed_value),
        1.0 / scale,
    )
    combined = ratio_error_bound + operation_ulp
    ratio_fixed = quantize_decimal(central, scale)
    ledger = {
        "kind": "drift_ratio",
        "join_id": join_id,
        "participating_tables": sorted(set(participating_tables)),
        "batch_interval": list(batch_interval),
        "observation_count": len(observation_points),
        "scale": scale,
        "actual": actual.to_dict(),
        "estimate": estimated.to_dict(),
        "ratio_fixed": ratio_fixed.to_dict(),
        "rounding_direction": ratio_fixed.rounding_direction,
        "ratio_error_interval": [ratio_low, ratio_high],
        "quantization_error_bound": ratio_error_bound,
        "floating_accumulation_bound": operation_ulp,
        "combined_error_bound": combined,
        "error_source_chain": {
            "source_table": source_table,
            "source_key": source_key,
            "participating_tables": sorted(set(participating_tables)),
            "batch_interval": list(batch_interval),
            "scale": scale,
            "rounding_direction": ratio_fixed.rounding_direction,
            "observation_rounding_directions": [
                point.get("rounding_direction") for point in observation_points
            ],
            "formula": "abs(fixed(actual)-fixed(estimate))/max(fixed(estimate),1/scale)",
        },
        "observations": list(observation_points),
    }
    return central, ledger


def precision_evidence(
    scenario: Scenario,
    *,
    winner: Mapping[str, Any],
    runner_up: Mapping[str, Any],
    observations: Iterable[Mapping[str, Any]],
    batch_interval: Sequence[str],
) -> dict[str, Any]:
    scale = scenario.config.fixed_scale
    involved_tables = sorted(
        {
            table
            for item in (winner, runner_up)
            for table in item.get("join_order", [])
        }
    )
    direction_counts: dict[str, int] = {}
    for observation in observations:
        direction = str(observation.get("rounding_direction", "UNKNOWN"))
        direction_counts[direction] = direction_counts.get(direction, 0) + 1
    return {
        "scale": scale,
        "tolerance": scenario.config.float_tolerance,
        "participating_tables": involved_tables,
        "batch_interval": list(batch_interval),
        "rounding_direction_counts": direction_counts,
        "error_source_chain": [
            {
                "table": table,
                "batch_interval": list(batch_interval),
                "scale": scale,
                "rounding_directions": direction_counts,
                "source": "committed_batch_observations",
            }
            for table in involved_tables
        ],
    }

