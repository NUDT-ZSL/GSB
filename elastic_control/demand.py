from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Any

from .models import ServiceSpec, WorkloadWindow


@dataclass(frozen=True)
class DemandDecision:
    service_name: str
    tick: int
    target_replicas: int
    estimated_replicas_before_clamp: int
    estimated_tokens_per_tick: str
    throughput_tokens_per_tick_per_replica: str
    throughput_source: str
    ceil_rule: str
    clamp: dict[str, int]
    aggregation_order: tuple[str, ...]
    observations: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "tick": self.tick,
            "target_replicas": self.target_replicas,
            "estimated_replicas_before_clamp": self.estimated_replicas_before_clamp,
            "estimated_tokens_per_tick": self.estimated_tokens_per_tick,
            "throughput_tokens_per_tick_per_replica": (
                self.throughput_tokens_per_tick_per_replica
            ),
            "throughput_source": self.throughput_source,
            "ceil_rule": self.ceil_rule,
            "clamp": self.clamp,
            "aggregation_order": list(self.aggregation_order),
            "observations": self.observations,
        }


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def choose_target_replicas(
    service: ServiceSpec,
    window: WorkloadWindow,
) -> DemandDecision:
    """Compute a deterministic target without binary floating-point arithmetic.

    The single canonical ceil location is after dividing workload by per-replica
    throughput.  All inputs are converted from their shortest JSON decimal
    representation and all arithmetic runs at fixed 50-digit precision.
    """

    with localcontext() as context:
        context.prec = 50
        arrival = _decimal(window.arrival_rate)
        sequence = _decimal(window.avg_sequence_length)
        throughput = _decimal(service.throughput_tokens_per_tick)

        required_tokens = arrival * sequence
        raw_replicas = required_tokens / throughput
        estimated = int(raw_replicas.to_integral_value(rounding=ROUND_CEILING))

    target = max(service.min_replicas, min(service.max_replicas, estimated))
    clamp_reason: str | None = None
    if estimated < service.min_replicas:
        clamp_reason = "min_replicas"
    elif estimated > service.max_replicas:
        clamp_reason = "max_replicas"

    return DemandDecision(
        service_name=service.name,
        tick=window.tick,
        target_replicas=target,
        estimated_replicas_before_clamp=estimated,
        estimated_tokens_per_tick=str(required_tokens),
        throughput_tokens_per_tick_per_replica=str(throughput),
        throughput_source=service.throughput_source,
        ceil_rule=(
            "ceil(arrival_rate * avg_sequence_length / "
            "throughput_tokens_per_tick_per_replica)"
        ),
        clamp={
            "min_replicas": service.min_replicas,
            "max_replicas": service.max_replicas,
            "applied": clamp_reason or "none",
        },
        aggregation_order=(
            "multiply arrival_rate by avg_sequence_length",
            "divide required_tokens_per_tick by per-replica throughput",
            "apply ROUND_CEILING exactly once",
            "clamp to [min_replicas, max_replicas]",
        ),
        observations={
            "arrival_rate": window.arrival_rate,
            "avg_sequence_length": window.avg_sequence_length,
            "tokens_per_tick_per_replica": service.throughput_tokens_per_tick,
            "batch_window_ms": service.batch_window_ms,
            "throughput_source": service.throughput_source,
        },
    )

