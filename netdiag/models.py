from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


Outcome = Literal[
    "success",
    "status_mismatch",
    "timeout",
    "connection_refused",
    "malformed_response",
    "abandoned",
]

AbandonKind = Literal["not_started_budget", "active_budget_cutoff"]
TargetState = Literal["healthy", "suspicious", "suspected_loss", "unreachable"]


@dataclass(frozen=True)
class Tolerance:
    loss_rate: float
    latency_ms: float


@dataclass(frozen=True)
class DiagnosticConfig:
    max_concurrency: int
    round_budget_ms: int
    window_size: int
    window_step: int
    loss_rate_threshold: float
    latency_jitter_threshold_ms: float
    tolerance: Tolerance
    clock_step_ms: int


@dataclass(frozen=True)
class TargetSpec:
    name: str
    endpoint: str
    expected_status: int
    period_ms: int
    timeout_ms: int
    failure_threshold: int


@dataclass(frozen=True)
class ProbeSpec:
    seq: int
    round_no: int
    target: str
    probe_type: str
    logical_tick: int
    plan_index: int


@dataclass(frozen=True)
class RoundSpec:
    round_no: int
    logical_tick: int
    probes: tuple[ProbeSpec, ...]


@dataclass(frozen=True)
class Scenario:
    schema_version: int
    scenario_id: str
    targets: tuple[TargetSpec, ...]
    rounds: tuple[RoundSpec, ...]
    config: DiagnosticConfig
    clock_step_table: tuple[int, ...]
    raw: dict[str, Any]

    def target_map(self) -> dict[str, TargetSpec]:
        return {target.name: target for target in self.targets}


@dataclass(frozen=True)
class ProbeObservation:
    outcome: Outcome
    logical_duration_ms: int
    response_status: int | None
    detail: str
    observed_latency_ms: int | None = None


@dataclass(frozen=True)
class ProbeRecord:
    config_version: int
    round_no: int
    target: str
    seq: int
    probe_type: str
    plan_index: int
    logical_start_ms: int
    logical_end_ms: int
    outcome: Outcome
    abandon_kind: AbandonKind | None
    counted_in_loss: bool
    excluded_from_loss: bool
    response_status: int | None
    expected_status: int
    observed_latency_ms: int | None
    detail: str

    @property
    def failure(self) -> bool:
        return self.outcome not in {"success", "abandoned"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "round_no": self.round_no,
            "target": self.target,
            "seq": self.seq,
            "probe_type": self.probe_type,
            "plan_index": self.plan_index,
            "logical_start_ms": self.logical_start_ms,
            "logical_end_ms": self.logical_end_ms,
            "outcome": self.outcome,
            "abandon_kind": self.abandon_kind,
            "counted_in_loss": self.counted_in_loss,
            "excluded_from_loss": self.excluded_from_loss,
            "response_status": self.response_status,
            "expected_status": self.expected_status,
            "observed_latency_ms": self.observed_latency_ms,
            "detail": self.detail,
        }
