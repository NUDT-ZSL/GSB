from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    localcontext,
)
from typing import Any, Iterable

from .errors import fail
from .validation import ModelSpec, PrecisionConfig, RequestSpec, ReplicaSpec


ROUNDING_MAP = {
    "half_even": ROUND_HALF_EVEN,
    "half_up": ROUND_HALF_UP,
    "floor": ROUND_FLOOR,
    "ceiling": ROUND_CEILING,
}


@dataclass
class Tracked:
    """A float value paired with an absolute first-order error bound."""

    value: float
    error: float = 0.0
    trace: list[dict[str, Any]] = field(default_factory=list)

    def clone(self) -> "Tracked":
        return Tracked(value=self.value, error=self.error, trace=list(self.trace))


@dataclass(frozen=True)
class FixedQuantity:
    raw: float
    fixed: int
    scale: int
    rounding_mode: str
    rounding_direction: str
    rounding_error: float
    accumulated_error_bound: float
    within_budget: bool
    trace: tuple[dict[str, Any], ...]

    @property
    def as_float(self) -> float:
        return self.fixed / float(self.scale)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "fixed": self.fixed,
            "scale": self.scale,
            "scaled_float": self.as_float,
            "rounding_mode": self.rounding_mode,
            "rounding_direction": self.rounding_direction,
            "rounding_error": self.rounding_error,
            "accumulated_error_bound": self.accumulated_error_bound,
            "within_error_budget": self.within_budget,
            "trace": list(self.trace),
        }


@dataclass(frozen=True)
class BatchEstimate:
    compute: FixedQuantity
    memory: FixedQuantity
    queue_ms: FixedQuantity
    efficiency: FixedQuantity
    priority_sum: int
    aggregation_order: tuple[int, ...]
    request_ids: tuple[str, ...]
    observations: dict[str, Any]
    estimated_completion_ms: float
    within_budget: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "compute": self.compute.to_dict(),
            "memory": self.memory.to_dict(),
            "queue_ms": self.queue_ms.to_dict(),
            "efficiency": self.efficiency.to_dict(),
            "priority_sum": self.priority_sum,
            "aggregation_order": list(self.aggregation_order),
            "request_ids": list(self.request_ids),
            "observations": self.observations,
            "estimated_completion_ms": self.estimated_completion_ms,
            "within_error_budget": self.within_budget,
        }

    def comparison_dict(self) -> dict[str, Any]:
        return {
            "compute": self.compute.to_dict(),
            "memory": self.memory.to_dict(),
            "queue_ms": self.queue_ms.to_dict(),
            "efficiency": self.efficiency.to_dict(),
            "priority_sum": self.priority_sum,
            "aggregation_order": list(self.aggregation_order),
            "request_ids": list(self.request_ids),
            "estimated_completion_ms": self.estimated_completion_ms,
        }


def _roundoff(value: float) -> float:
    if not math.isfinite(value):
        fail("E_NUMERIC", "non-finite value encountered during estimation")
    return 0.5 * math.ulp(value)


def add(a: Tracked, b: Tracked, label: str, request_ids: Iterable[int] = ()) -> Tracked:
    value = a.value + b.value
    roundoff = _roundoff(value)
    error = a.error + b.error + roundoff
    reqs = sorted(set(request_ids))
    trace = list(a.trace)
    trace.extend(b.trace)
    trace.append(
        {
            "step": len(trace) + 1,
            "op": "add",
            "label": label,
            "left": a.value,
            "right": b.value,
            "result": value,
            "roundoff_bound": roundoff,
            "error_in": a.error + b.error,
            "error_out": error,
            "request_seqs": reqs,
        }
    )
    return Tracked(value=value, error=error, trace=trace)


def subtract(
    a: Tracked, b: Tracked, label: str, request_ids: Iterable[int] = ()
) -> Tracked:
    value = a.value - b.value
    roundoff = _roundoff(value)
    error = a.error + b.error + roundoff
    reqs = sorted(set(request_ids))
    trace = list(a.trace)
    trace.extend(b.trace)
    trace.append(
        {
            "step": len(trace) + 1,
            "op": "subtract",
            "label": label,
            "left": a.value,
            "right": b.value,
            "result": value,
            "roundoff_bound": roundoff,
            "error_in": a.error + b.error,
            "error_out": error,
            "request_seqs": reqs,
        }
    )
    return Tracked(value=value, error=error, trace=trace)


def multiply(
    a: Tracked, b: Tracked, label: str, request_ids: Iterable[int] = ()
) -> Tracked:
    value = a.value * b.value
    roundoff = _roundoff(value)
    error = abs(a.value) * b.error + abs(b.value) * a.error + a.error * b.error + roundoff
    reqs = sorted(set(request_ids))
    trace = list(a.trace)
    trace.extend(b.trace)
    trace.append(
        {
            "step": len(trace) + 1,
            "op": "multiply",
            "label": label,
            "left": a.value,
            "right": b.value,
            "result": value,
            "roundoff_bound": roundoff,
            "error_in": abs(a.value) * b.error + abs(b.value) * a.error,
            "error_out": error,
            "request_seqs": reqs,
        }
    )
    return Tracked(value=value, error=error, trace=trace)


def divide(
    a: Tracked, b: Tracked, label: str, request_ids: Iterable[int] = ()
) -> Tracked:
    if b.value == 0.0:
        fail("E_NUMERIC", "division by zero during estimation", label=label)
    value = a.value / b.value
    roundoff = _roundoff(value)
    error = a.error / abs(b.value) + abs(value) * b.error / abs(b.value) + roundoff
    reqs = sorted(set(request_ids))
    trace = list(a.trace)
    trace.extend(b.trace)
    trace.append(
        {
            "step": len(trace) + 1,
            "op": "divide",
            "label": label,
            "left": a.value,
            "right": b.value,
            "result": value,
            "roundoff_bound": roundoff,
            "error_in": a.error / abs(b.value)
            + abs(value) * b.error / abs(b.value),
            "error_out": error,
            "request_seqs": reqs,
        }
    )
    return Tracked(value=value, error=error, trace=trace)


def constant(
    value: float,
    label: str,
    request_ids: Iterable[int] = (),
    source: str = "parameter",
) -> Tracked:
    return Tracked(
        value=float(value),
        error=0.0,
        trace=[
            {
                "step": 1,
                "op": "constant",
                "label": label,
                "result": float(value),
                "source": source,
                "request_seqs": sorted(set(request_ids)),
            }
        ],
    )


def convert_fixed(
    tracked: Tracked,
    scale: int,
    rounding_mode: str,
    label: str,
    error_budget: float,
) -> FixedQuantity:
    with localcontext() as context:
        context.prec = 80
        decimal_raw = Decimal.from_float(tracked.value)
        scaled = decimal_raw * Decimal(scale)
        fixed = int(
            scaled.to_integral_value(rounding=ROUNDING_MAP[rounding_mode])
        )
        quantized = Decimal(fixed) / Decimal(scale)
        conversion_error = float(abs(decimal_raw - quantized))
    if quantized > decimal_raw:
        direction = "up"
    elif quantized < decimal_raw:
        direction = "down"
    else:
        direction = "none"
    accumulated = tracked.error + conversion_error
    trace = list(tracked.trace)
    trace.append(
        {
            "step": len(trace) + 1,
            "op": "fixed_convert",
            "label": label,
            "raw": tracked.value,
            "scale": scale,
            "quantized": format(quantized, "f"),
            "fixed": fixed,
            "rounding_mode": rounding_mode,
            "rounding_direction": direction,
            "conversion_error": conversion_error,
            "error_in": tracked.error,
            "error_out": accumulated,
        }
    )
    return FixedQuantity(
        raw=tracked.value,
        fixed=fixed,
        scale=scale,
        rounding_mode=rounding_mode,
        rounding_direction=direction,
        rounding_error=conversion_error,
        accumulated_error_bound=accumulated,
        within_budget=accumulated <= error_budget,
        trace=tuple(trace),
    )


def estimate_batch(
    requests: list[RequestSpec],
    model: ModelSpec,
    replica: ReplicaSpec,
    precision: PrecisionConfig,
    *,
    window_start_ms: int,
    window_end_ms: int,
    decision_clock_ms: int,
) -> BatchEstimate:
    """Estimate one batch using a single documented left-to-right operation order."""

    if not requests:
        fail("E_BAD_BATCH", "cannot estimate an empty batch")

    compute = constant(
        model.batch_base_compute_units, "model.batch_base_compute_units"
    )
    memory = add(
        constant(model.weights_mib, "model.weights_mib"),
        constant(model.activation_base_mib, "model.activation_base_mib"),
        "memory.weights_plus_activation",
    )
    priority_sum = Tracked(value=0.0)

    for request in requests:
        req_ids = (request.request_seq,)
        total_tokens = constant(
            request.total_tokens,
            "request.total_tokens",
            req_ids,
            source=f"request:{request.request_id}",
        )
        param_cost = multiply(
            constant(
                model.total_params * model.layer_param_units_per_token,
                "model.param_units_per_token",
                source="model.layer_parameters",
            ),
            total_tokens,
            "compute.layer_params",
            req_ids,
        )
        input_cost = multiply(
            constant(
                request.input_tokens,
                "request.input_tokens",
                req_ids,
                source=f"request:{request.request_id}",
            ),
            constant(
                model.input_token_compute_units,
                "model.input_token_compute_units",
                source="model.compute",
            ),
            "compute.input_tokens",
            req_ids,
        )
        output_cost = multiply(
            constant(
                request.max_output_tokens,
                "request.max_output_tokens",
                req_ids,
                source=f"request:{request.request_id}",
            ),
            constant(
                model.output_token_compute_units,
                "model.output_token_compute_units",
                source="model.compute",
            ),
            "compute.output_tokens",
            req_ids,
        )
        request_compute = add(
            param_cost, input_cost, "compute.params_plus_input", req_ids
        )
        request_compute = add(
            request_compute, output_cost, "compute.request_total", req_ids
        )
        compute = add(
            compute, request_compute, "compute.batch_aggregate", req_ids
        )

        request_memory = constant(
            model.request_base_mib,
            "model.request_base_mib",
            req_ids,
            source="model.memory",
        )
        input_kv = multiply(
            constant(
                request.input_tokens,
                "request.input_tokens",
                req_ids,
                source=f"request:{request.request_id}",
            ),
            constant(
                model.kv_input_mib_per_token,
                "model.kv_input_mib_per_token",
                source="model.memory",
            ),
            "memory.input_kv",
            req_ids,
        )
        output_kv = multiply(
            constant(
                request.max_output_tokens,
                "request.max_output_tokens",
                req_ids,
                source=f"request:{request.request_id}",
            ),
            constant(
                model.kv_output_mib_per_token,
                "model.kv_output_mib_per_token",
                source="model.memory",
            ),
            "memory.output_kv",
            req_ids,
        )
        request_memory = add(
            request_memory, input_kv, "memory.base_plus_input_kv", req_ids
        )
        request_memory = add(
            request_memory, output_kv, "memory.request_total", req_ids
        )
        memory = add(
            memory, request_memory, "memory.batch_aggregate", req_ids
        )
        priority_sum = add(
            priority_sum,
            constant(
                float(request.priority + 1),
                "request.priority_weight",
                req_ids,
                source=f"request:{request.request_id}",
            ),
            "priority_sum.aggregate",
            req_ids,
        )

    execution_ms = divide(
        compute,
        constant(replica.available_compute_units, "replica.available_compute_units"),
        "queue.compute_fraction",
    )
    execution_ms = multiply(
        execution_ms,
        constant(float(replica.batch_window_ms), "replica.batch_window_ms"),
        "queue.execution_ms",
    )
    execution_ms = add(
        execution_ms,
        constant(model.fixed_queue_latency_ms, "model.fixed_queue_latency_ms"),
        "queue.execution_plus_fixed",
    )
    remaining_ms = max(
        precision.logical_clock_step_ms, window_end_ms - decision_clock_ms
    )
    queue_ms = add(
        constant(float(remaining_ms), "window.remaining_logical_ms"),
        execution_ms,
        "queue.wait_plus_execution",
    )
    efficiency = divide(
        priority_sum,
        queue_ms,
        "candidate.priority_per_queue_ms",
    )

    first_arrival = min(request.arrival_ms for request in requests)
    observations = {
        "aggregation_order": [request.request_seq for request in requests],
        "aggregation_rule": (
            "priority_desc,arrival_ms_asc,request_seq_asc; "
            "left-to-right float64 arithmetic"
        ),
        "decision_clock_ms": decision_clock_ms,
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
        "window_remaining_logical_ms": remaining_ms,
        "first_arrival_ms": first_arrival,
        "replica_available_compute_units": replica.available_compute_units,
        "replica_memory_limit_mib": replica.memory_limit_mib,
        "replica_remaining_compute_units_before": (
            replica.available_compute_units
        ),
        "replica_remaining_memory_mib_before": float(
            replica.memory_limit_mib
        ),
        "replica_remaining_compute_units_after": (
            replica.available_compute_units - compute.value
        ),
        "replica_remaining_memory_mib_after": (
            float(replica.memory_limit_mib) - memory.value
        ),
        "error_budget": precision.error_budget,
        "replica_max_batch_size": replica.max_batch_size,
        "replica_max_sequence_length": replica.max_sequence_length,
        "request_tokens": [
            {
                "request_seq": request.request_seq,
                "request_id": request.request_id,
                "input_tokens": request.input_tokens,
                "max_output_tokens": request.max_output_tokens,
                "total_tokens": request.total_tokens,
                "priority": request.priority,
                "arrival_ms": request.arrival_ms,
                "deadline_ms": request.deadline_ms,
            }
            for request in requests
        ],
    }

    compute_fixed = convert_fixed(
        compute,
        precision.compute_scale,
        precision.rounding_mode,
        "batch.compute_units",
        precision.error_budget,
    )
    memory_fixed = convert_fixed(
        memory,
        precision.memory_scale,
        precision.rounding_mode,
        "batch.memory_mib",
        precision.error_budget,
    )
    queue_fixed = convert_fixed(
        queue_ms,
        precision.queue_ms_scale,
        precision.rounding_mode,
        "batch.queue_ms",
        precision.error_budget,
    )
    efficiency_fixed = convert_fixed(
        efficiency,
        precision.queue_ms_scale,
        precision.rounding_mode,
        "candidate.efficiency",
        precision.error_budget,
    )
    completion = float(decision_clock_ms) + queue_ms.value
    return BatchEstimate(
        compute=compute_fixed,
        memory=memory_fixed,
        queue_ms=queue_fixed,
        efficiency=efficiency_fixed,
        priority_sum=sum(request.priority + 1 for request in requests),
        aggregation_order=tuple(request.request_seq for request in requests),
        request_ids=tuple(request.request_id for request in requests),
        observations=observations,
        estimated_completion_ms=completion,
        within_budget=(
            compute_fixed.within_budget
            and memory_fixed.within_budget
            and queue_fixed.within_budget
            and efficiency_fixed.within_budget
        ),
    )

