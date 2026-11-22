"""Deterministic fixed-point conversion with an explicit error ledger."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Any, Iterable

from .models import PrecisionConfig, RequestSpec


@dataclass(frozen=True)
class Conversion:
    fixed: int
    raw: float
    scale: int
    direction: str
    conversion_error: float
    error_bound: float


def _round_decimal(value: Decimal, rounding: str) -> int:
    if rounding == "half_even":
        mode = ROUND_HALF_EVEN
    elif rounding == "toward_zero":
        mode = ROUND_DOWN
    else:
        mode = ROUND_HALF_UP
    return int(value.quantize(Decimal("1"), rounding=mode))


def quantize(raw: float, scale: int, rounding: str = "half_up") -> Conversion:
    if not math.isfinite(raw):
        raise ValueError("cannot quantize a non-finite value")
    decimal_value = Decimal(str(raw)) * Decimal(scale)
    fixed = _round_decimal(decimal_value, rounding)
    rendered = fixed / scale
    if rendered > raw:
        direction = "up"
    elif rendered < raw:
        direction = "down"
    else:
        direction = "exact"
    return Conversion(
        fixed=fixed,
        raw=raw,
        scale=scale,
        direction=direction,
        conversion_error=abs(raw - rendered),
        error_bound=(1.0 if rounding == "toward_zero" else 0.5) / scale,
    )


@dataclass
class LedgerOperation:
    op_id: str
    batch_id: str
    metric: str
    ordinal: int
    operation: str
    operand_a_raw: float | None
    operand_b_raw: float | None
    result_raw: float
    scale: int | None
    fixed_result: int | None
    rounding: str | None
    propagated_error_bound: float
    cumulative_error_bound: float
    details: dict[str, Any]


@dataclass
class MetricLedger:
    metric: str
    raw: float
    fixed: int
    scale: int
    error_bound: float
    operations: list[LedgerOperation]


class _MetricBuilder:
    def __init__(self, batch_id: str, metric: str) -> None:
        self.batch_id = batch_id
        self.metric = metric
        self.operations: list[LedgerOperation] = []
        self.cumulative = 0.0

    def record(
        self,
        operation: str,
        result_raw: float,
        *,
        operand_a: float | None = None,
        operand_b: float | None = None,
        bound: float = 0.0,
        scale: int | None = None,
        fixed: int | None = None,
        rounding: str | None = None,
        details: dict[str, Any] | None = None,
        conversion: Conversion | None = None,
    ) -> None:
        if conversion is not None:
            scale = conversion.scale
            fixed = conversion.fixed
            rounding = conversion.direction
            bound = max(bound, conversion.error_bound)
            details = dict(details or {})
            details["observed_conversion_error"] = conversion.conversion_error
        self.cumulative += max(0.0, bound)
        ordinal = len(self.operations) + 1
        self.operations.append(
            LedgerOperation(
                op_id=f"{self.batch_id}:{self.metric}:{ordinal:03d}",
                batch_id=self.batch_id,
                metric=self.metric,
                ordinal=ordinal,
                operation=operation,
                operand_a_raw=operand_a,
                operand_b_raw=operand_b,
                result_raw=result_raw,
                scale=scale,
                fixed_result=fixed,
                rounding=rounding,
                propagated_error_bound=max(0.0, bound),
                cumulative_error_bound=self.cumulative,
                details=details or {},
            )
        )

    def finish(self, conversion: Conversion) -> MetricLedger:
        self.record(
            "convert_fixed",
            conversion.raw,
            conversion=conversion,
            details={
                "scale": conversion.scale,
                "rounding_direction": conversion.direction,
                "raw_before_conversion": conversion.raw,
            },
        )
        return MetricLedger(
            metric=self.metric,
            raw=conversion.raw,
            fixed=conversion.fixed,
            scale=conversion.scale,
            error_bound=self.cumulative,
            operations=self.operations,
        )


def _ulp_bound(value: float) -> float:
    if value == 0.0:
        return 0.0
    return abs(math.ulp(value)) * 0.5


def estimate_request_compute(model_parameters: int, request: RequestSpec) -> float:
    return 2.0 * float(model_parameters) * float(request.total_tokens) / 1e9


def estimate_request_activation_mib(
    activation_bytes_per_token: int, request: RequestSpec
) -> float:
    return request.total_tokens * activation_bytes_per_token / (1024.0 * 1024.0)


def build_compute_ledger(
    batch_id: str,
    model_parameters: int,
    requests: Iterable[RequestSpec],
    precision: PrecisionConfig,
) -> MetricLedger:
    builder = _MetricBuilder(batch_id, "compute")
    total_tokens = sum(request.total_tokens for request in requests)
    builder.record(
        "add",
        float(total_tokens),
        bound=0.0,
        details={"operation": "sum_request_token_counts", "exact_integer_operation": True},
    )
    product = model_parameters * total_tokens
    builder.record(
        "multiply",
        float(product),
        operand_a=float(model_parameters),
        operand_b=float(total_tokens),
        bound=0.0,
        details={"exact_integer_operation": True},
    )
    doubled = product * 2
    builder.record(
        "multiply",
        float(doubled),
        operand_a=float(product),
        operand_b=2.0,
        bound=0.0,
        details={"exact_integer_operation": True},
    )
    raw = float(doubled) / 1e9
    builder.record(
        "divide",
        raw,
        operand_a=float(doubled),
        operand_b=1e9,
        bound=_ulp_bound(raw),
        details={"divisor": 1e9},
    )
    conversion = quantize(raw, precision.compute_scale, precision.rounding)
    return builder.finish(conversion)


def build_memory_ledger(
    batch_id: str,
    model_parameters: int,
    bytes_per_parameter: int,
    activation_bytes_per_token: int,
    requests: Iterable[RequestSpec],
    precision: PrecisionConfig,
) -> MetricLedger:
    builder = _MetricBuilder(batch_id, "memory")
    weights_bytes = model_parameters * bytes_per_parameter
    builder.record(
        "multiply",
        float(weights_bytes),
        operand_a=float(model_parameters),
        operand_b=float(bytes_per_parameter),
        bound=0.0,
        details={"exact_integer_operation": True},
    )
    weights_mib = float(weights_bytes) / (1024.0 * 1024.0)
    builder.record(
        "divide",
        weights_mib,
        operand_a=float(weights_bytes),
        operand_b=1024.0 * 1024.0,
        bound=_ulp_bound(weights_mib),
        details={"unit": "MiB"},
    )
    activation_total = 0.0
    for request in requests:
        activation_bytes = request.total_tokens * activation_bytes_per_token
        builder.record(
            "multiply",
            float(activation_bytes),
            operand_a=float(request.total_tokens),
            operand_b=float(activation_bytes_per_token),
            bound=0.0,
            details={"request_seq": request.seq, "exact_integer_operation": True},
        )
        activation_mib = float(activation_bytes) / (1024.0 * 1024.0)
        builder.record(
            "divide",
            activation_mib,
            operand_a=float(activation_bytes),
            operand_b=1024.0 * 1024.0,
            bound=_ulp_bound(activation_mib),
            details={"request_seq": request.seq, "unit": "MiB"},
        )
        previous = activation_total
        activation_total += activation_mib
        builder.record(
            "add",
            activation_total,
            operand_a=previous,
            operand_b=activation_mib,
            bound=_ulp_bound(activation_total),
            details={"request_seq": request.seq},
        )
    raw = weights_mib + activation_total
    builder.record(
        "add",
        raw,
        operand_a=weights_mib,
        operand_b=activation_total,
        bound=_ulp_bound(raw),
        details={"components": ["weights", "activations"]},
    )
    conversion = quantize(raw, precision.memory_scale, precision.rounding)
    return builder.finish(conversion)


def build_latency_ledger(
    batch_id: str,
    requests: Iterable[RequestSpec],
    window_end_ms: int,
    compute_raw: float,
    latency_ms_per_compute_unit: float,
    precision: PrecisionConfig,
) -> MetricLedger:
    builder = _MetricBuilder(batch_id, "latency")
    request_list = list(requests)
    wait_sum = 0.0
    for request in request_list:
        wait = float(max(0, window_end_ms - request.arrival_ms))
        previous = wait_sum
        wait_sum += wait
        builder.record(
            "add",
            wait_sum,
            operand_a=previous,
            operand_b=wait,
            bound=_ulp_bound(wait_sum),
            details={"request_seq": request.seq, "operation": "sum_window_wait"},
        )
    mean_wait = wait_sum / len(request_list)
    builder.record(
        "divide",
        mean_wait,
        operand_a=wait_sum,
        operand_b=float(len(request_list)),
        bound=_ulp_bound(mean_wait),
        details={"operation": "mean_window_wait"},
    )
    compute_delay = compute_raw * latency_ms_per_compute_unit
    builder.record(
        "multiply",
        compute_delay,
        operand_a=compute_raw,
        operand_b=latency_ms_per_compute_unit,
        bound=_ulp_bound(compute_delay),
        details={"operation": "compute_latency"},
    )
    raw = mean_wait + compute_delay
    builder.record(
        "add",
        raw,
        operand_a=mean_wait,
        operand_b=compute_delay,
        bound=_ulp_bound(raw),
        details={"components": ["mean_window_wait", "compute_latency"]},
    )
    conversion = quantize(raw, precision.latency_scale, precision.rounding)
    return builder.finish(conversion)
