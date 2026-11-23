from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from .models import Observation, ServiceSpec


@dataclass(frozen=True)
class TargetConversion:
    service: str
    target_replicas: int
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "target_replicas": self.target_replicas,
            "evidence": self.evidence,
        }


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def convert_target(
    service: ServiceSpec, observation: Observation, logical_clock_ms: int
) -> TargetConversion:
    demand = _decimal(observation.arrival_rate) * _decimal(
        observation.avg_sequence_length
    )
    capacity = service.replica_capacity_tokens_per_second
    raw_ratio = demand / capacity
    ceil_target = int(raw_ratio.to_integral_value(rounding=ROUND_CEILING))
    clamped = max(service.min_replicas, min(service.max_replicas, ceil_target))
    evidence = {
        "logical_clock_ms": logical_clock_ms,
        "observation": observation.to_dict(),
        "replica_capacity_tokens_per_second": str(capacity),
        "demand_tokens_per_second": str(demand),
        "raw_ratio": str(raw_ratio),
        "ceil_input": str(raw_ratio),
        "ceil_applied_to": "post_division_ratio",
        "ceil_result": ceil_target,
        "clamped_result": clamped,
        "min_replicas": service.min_replicas,
        "max_replicas": service.max_replicas,
        "batch_window_ms": service.batch_window_ms,
        "aggregation_order": [
            "arrival_rate * avg_sequence_length -> demand_tokens_per_second",
            "replica_compute_units * tokens_per_second_per_compute_unit -> replica_capacity",
            "demand_tokens_per_second / replica_capacity -> raw_ratio",
            "ceil(raw_ratio) -> unclamped_replicas",
            "clamp(min_replicas, unclamped_replicas, max_replicas)",
        ],
        "formula": (
            "clamp(ceil(arrival_rate * avg_sequence_length / "
            "(replica_compute_units * tokens_per_second_per_compute_unit)))"
        ),
    }
    return TargetConversion(
        service=service.name,
        target_replicas=clamped,
        evidence=evidence,
    )
