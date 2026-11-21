from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Any, Iterable

from .config import ModelSpec, PrecisionSpec, ReplicaSpec, RequestSpec

EPS = sys.float_info.epsilon


@dataclass(frozen=True)
class ErrorValue:
    value: float
    bound: float
    chain: tuple[str, ...]
    operations: tuple[dict[str, Any], ...] = ()


def literal(value: float, name: str) -> ErrorValue:
    return ErrorValue(float(value), 0.0, (name,))


def _op(
    kind: str,
    left: ErrorValue,
    right: ErrorValue,
    result: float,
    bound: float,
    label: str,
) -> ErrorValue:
    operation = {
        "kind": kind,
        "label": label,
        "left": left.value,
        "right": right.value,
        "result": result,
        "input_error_bound": left.bound + right.bound,
        "operation_error_bound": bound - left.bound - right.bound,
        "accumulated_error_bound": bound,
    }
    return ErrorValue(
        value=result,
        bound=max(0.0, bound),
        chain=left.chain + right.chain + (label,),
        operations=left.operations + right.operations + (operation,),
    )


def add(left: ErrorValue, right: ErrorValue, label: str) -> ErrorValue:
    result = left.value + right.value
    bound = left.bound + right.bound + EPS * abs(result)
    return _op("add", left, right, result, bound, label)


def multiply(left: ErrorValue, right: ErrorValue, label: str) -> ErrorValue:
    result = left.value * right.value
    bound = (
        abs(left.value) * right.bound
        + abs(right.value) * left.bound
        + EPS * abs(result)
    )
    return _op("multiply", left, right, result, bound, label)


def divide(left: ErrorValue, right: ErrorValue, label: str) -> ErrorValue:
    result = left.value / right.value
    bound = (
        left.bound / abs(right.value)
        + abs(result) * right.bound / abs(right.value)
        + EPS * abs(result)
    )
    return _op("divide", left, right, result, bound, label)


def sum_values(values: list[ErrorValue], label: str) -> ErrorValue:
    total = literal(0.0, f"{label}:zero")
    for index, value in enumerate(values):
        total = add(total, value, f"{label}:add:{index}")
    return total


def round_half_up_scaled(value: float) -> int:
    scaled = value
    if scaled >= 0:
        return math.floor(scaled + 0.5)
    return math.ceil(scaled - 0.5)


def fixed_record(
    metric: str,
    estimate: ErrorValue,
    scale: int,
    error_budget: float,
    aggregate_order: list[str],
) -> dict[str, Any]:
    scaled_value = estimate.value * scale
    fixed_value = round_half_up_scaled(scaled_value)
    if fixed_value > scaled_value:
        direction = "up"
    elif fixed_value < scaled_value:
        direction = "down"
    else:
        direction = "exact"
    quantization_bound = abs(scaled_value - fixed_value) / scale
    total_bound = estimate.bound + quantization_bound
    return {
        "metric": metric,
        "scale": scale,
        "raw_value": estimate.value,
        "scaled_raw_value": scaled_value,
        "fixed_value": fixed_value,
        "rounding_direction": direction,
        "quantization_error_bound": quantization_bound,
        "arithmetic_error_bound": estimate.bound,
        "total_error_bound": total_bound,
        "error_budget": error_budget,
        "within_error_budget": total_bound <= error_budget,
        "source_chain": list(estimate.chain),
        "operation_order": list(estimate.operations),
        "aggregate_order": aggregate_order,
    }


class Estimator:
    def __init__(self, model: ModelSpec, precision: PrecisionSpec) -> None:
        self.model = model
        self.precision = precision
        self._params_m = divide(
            literal(model.total_parameters, "model:total_parameters"),
            literal(1_000_000.0, "constant:one_million"),
            "model:parameters_in_millions",
        )
        self._compute_factor = literal(
            model.compute_units_per_million_parameters_per_token,
            "model:compute_factor",
        )
        self._memory_params_factor = literal(
            model.memory_mib_per_million_parameters,
            "model:memory_per_million_parameters",
        )
        self._memory_token_factor = literal(
            model.memory_mib_per_token, "model:memory_mib_per_token"
        )

    def request_estimate(self, request: RequestSpec) -> dict[str, ErrorValue]:
        tokens = literal(float(request.total_tokens), f"request:{request.request_seq}:tokens")
        compute = multiply(
            self._params_m,
            tokens,
            f"request:{request.request_seq}:compute:params_x_tokens",
        )
        compute = multiply(
            compute,
            self._compute_factor,
            f"request:{request.request_seq}:compute:x_factor",
        )
        memory_params = multiply(
            self._params_m,
            self._memory_params_factor,
            f"request:{request.request_seq}:memory:params",
        )
        memory_tokens = multiply(
            tokens,
            self._memory_token_factor,
            f"request:{request.request_seq}:memory:tokens",
        )
        memory = add(
            memory_params,
            memory_tokens,
            f"request:{request.request_seq}:memory:sum",
        )
        return {"compute": compute, "memory": memory}

    def batch_estimate(
        self,
        request_estimates: list[tuple[RequestSpec, dict[str, ErrorValue]]],
        replica: ReplicaSpec,
        aggregate_order: list[str],
    ) -> dict[str, ErrorValue]:
        compute_parts = [estimate["compute"] for _, estimate in request_estimates]
        memory_parts = [estimate["memory"] for _, estimate in request_estimates]
        compute = sum_values(compute_parts, "batch:compute")
        memory = sum_values(memory_parts, "batch:memory")
        queue_numerator = multiply(
            compute,
            literal(1000.0, "constant:milliseconds_per_second"),
            "batch:queue:compute_x_1000",
        )
        queue = divide(
            queue_numerator,
            literal(replica.compute_units, f"replica:{replica.replica_id}:compute_units"),
            "batch:queue:divide_by_replica_compute",
        )
        return {"compute": compute, "memory": memory, "queue": queue}

    def batch_precision_records(
        self,
        estimates: dict[str, ErrorValue],
        aggregate_order: list[str],
    ) -> list[dict[str, Any]]:
        return [
            fixed_record(
                "compute",
                estimates["compute"],
                self.precision.compute_scale,
                self.precision.error_bound_budget,
                aggregate_order,
            ),
            fixed_record(
                "memory",
                estimates["memory"],
                self.precision.memory_scale,
                self.precision.error_bound_budget,
                aggregate_order,
            ),
            fixed_record(
                "queue",
                estimates["queue"],
                self.precision.queue_scale,
                self.precision.error_bound_budget,
                aggregate_order,
            ),
        ]


def within_relative_tolerance(left: float, right: float, tolerance: float) -> bool:
    denominator = max(1.0, abs(left), abs(right))
    return abs(left - right) <= tolerance * denominator


def sensitivity_chain(
    decision: str,
    metric: str,
    left_name: str,
    left: float,
    right_name: str,
    right: float,
    tolerance: float,
    source_chain: Iterable[str],
) -> dict[str, Any]:
    return {
        "decision": decision,
        "metric": metric,
        "left": {"name": left_name, "value": left},
        "right": {"name": right_name, "value": right},
        "absolute_gap": abs(left - right),
        "relative_tolerance": tolerance,
        "near_tolerance_boundary": True,
        "depends_on_difference": True,
        "source_chain": list(source_chain),
    }

