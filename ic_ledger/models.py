"""Immutable input and runtime models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any


def queue_key(iface: str, queue: int) -> str:
    return f"{iface}:{queue}"


def fraction_pair(value: Fraction) -> tuple[int, int]:
    return value.numerator, value.denominator


def parse_fraction(value: Fraction | str | int) -> Fraction:
    if isinstance(value, Fraction):
        return value
    return Fraction(str(value))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CoalesceParams:
    rx_usecs: int
    rx_frames: int
    tx_usecs: int
    tx_frames: int

    def for_direction(self, direction: str) -> tuple[int, int]:
        if direction == "rx":
            return self.rx_usecs, self.rx_frames
        return self.tx_usecs, self.tx_frames

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class QueueSpec:
    iface: str
    queue: int
    weight: int
    max_irq_rate: int
    params: CoalesceParams

    @property
    def key(self) -> str:
        return queue_key(self.iface, self.queue)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "iface": self.iface,
            "queue": self.queue,
            "weight": self.weight,
            "max_irq_rate": self.max_irq_rate,
            "params": self.params.as_dict(),
        }

    def coalesce_dict(self) -> dict[str, Any]:
        return {
            "iface": self.iface,
            "queue": self.queue,
            "params": self.params.as_dict(),
        }


@dataclass(frozen=True)
class FlowRecord:
    record_seq: int
    iface: str
    queue: int
    logical_clock_us: int
    arrival_frames: int
    frame_bytes: int
    direction: str = "rx"

    @property
    def key(self) -> str:
        return queue_key(self.iface, self.queue)

    def input_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EngineConfig:
    fixed_scale: int
    avg_frame_interval_us: Fraction
    float_tolerance: Fraction
    error_bound_budget_us: Fraction
    clock_step_us: int
    irq_budget_per_second: dict[str, int] = field(default_factory=dict)

    def simulation_dict(self) -> dict[str, Any]:
        return {
            "avg_frame_interval_us": str(self.avg_frame_interval_us),
            "clock_step_us": self.clock_step_us,
            "fixed_scale": self.fixed_scale,
            "float_tolerance": str(self.float_tolerance),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "avg_frame_interval_us": str(self.avg_frame_interval_us),
            "clock_step_us": self.clock_step_us,
            "error_bound_budget_us": str(self.error_bound_budget_us),
            "fixed_scale": self.fixed_scale,
            "float_tolerance": str(self.float_tolerance),
            "irq_budget_per_second": dict(sorted(self.irq_budget_per_second.items())),
        }

    def budget_for(self, iface: str, queue: int, fallback: int) -> int:
        return self.irq_budget_per_second.get(queue_key(iface, queue), fallback)


@dataclass(frozen=True)
class SampleDocument:
    queues: tuple[QueueSpec, ...]
    flows: tuple[FlowRecord, ...]
    config: EngineConfig
    input_hash: str
    raw: dict[str, Any]

    def queue_map(self) -> dict[str, QueueSpec]:
        return {item.key: item for item in self.queues}

    def sorted_queues(self) -> list[QueueSpec]:
        return sorted(self.queues, key=lambda item: (item.iface, item.queue))


@dataclass
class ConversionTrace:
    path: str
    param_name: str
    scale: int
    exact_num: int
    exact_den: int
    rounded_units: int
    rounding: str
    discarded: Fraction
    residue_before: Fraction
    residue_after: Fraction
    float_value: float
    record_seq: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "discarded": str(self.discarded),
            "exact": f"{self.exact_num}/{self.exact_den}",
            "float_value": self.float_value,
            "param_name": self.param_name,
            "path": self.path,
            "record_seq": self.record_seq,
            "residue_after": str(self.residue_after),
            "residue_before": str(self.residue_before),
            "rounded_units": self.rounded_units,
            "rounding": self.rounding,
            "scale": self.scale,
        }


@dataclass
class PathState:
    window_start_us: int | None = None
    frames: int = 0
    interrupts: int = 0
    threshold_ticks: int = 0
    threshold_us: float = 0.0


@dataclass
class QueueRuntime:
    iface: str
    queue: int
    direction: str
    param_version: int
    first_clock_us: int | None = None
    last_clock_us: int | None = None
    last_record_seq: int | None = None
    total_frames: int = 0
    total_bytes: int = 0
    fixed: PathState = field(default_factory=PathState)
    float_state: PathState = field(default_factory=PathState)
    residue: Fraction = Fraction(0)
    discarded_total: Fraction = Fraction(0)
    error_bound_ticks: Fraction = Fraction(0)
    diverged: bool = False
    first_divergence_record: int | None = None
    first_divergence_metric: str | None = None

    @property
    def key(self) -> str:
        return queue_key(self.iface, self.queue)

