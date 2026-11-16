"""Core data models for scenes, samples, aggregated metrics and snapshots."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

# Status vocabulary for a stage verdict.
VERDICT_PASS = "pass"
VERDICT_SATURATED = "saturated"
VERDICT_UNKNOWN = "unknown"          # interrupted stage; never reported as pass/fail
SATURATION_FAILURE_RATE = "failure_rate_exceeded"
SATURATION_LATENCY = "latency_p95_exceeded"
SATURATION_BOTH = "both_failure_rate_and_latency"


@dataclass(frozen=True)
class StageSpec:
    index: int                 # 1-based, consecutive and unique
    duration_s: int            # positive integer seconds
    concurrency: int           # positive integer connections
    throughput_limit_rps: int  # positive integer per-stage cap


@dataclass(frozen=True)
class SloThresholds:
    min_success_rate_bp: int   # basis points, 0..10000
    max_p95_latency_ms: int    # positive integer milliseconds


@dataclass(frozen=True)
class TargetSpec:
    host: str
    port: int

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class Scene:
    name: str
    target: TargetSpec
    stages: tuple[StageSpec, ...]
    slo: SloThresholds
    sample_interval_ms: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sample:
    """One fixed-interval sample row. All counters are deltas for the interval."""
    stage_index: int
    tick: int
    ts_ms: int
    success: int
    failure: int
    latency_ms: list[int]     # latency of every completed request in interval

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Observation:
    """Full observation record of one finished stage (the raw material)."""
    stage_index: int
    started_at_ms: int
    ended_at_ms: int
    samples: list[Sample]
    server_confirmed_success: int
    server_confirmed_failure: int
    clock_basis: str
    truth_source: str

    @property
    def duration_ms(self) -> int:
        return self.ended_at_ms - self.started_at_ms

    @property
    def total_success(self) -> int:
        return sum(s.success for s in self.samples)

    @property
    def total_failure(self) -> int:
        return sum(s.failure for s in self.samples)


@dataclass
class StageMetrics:
    """Fixed-point metric triple with explicit quantization error bounds."""
    success_rate_bp: int           # basis points (1bp = 0.01%)
    p95_latency_ms: int            # nearest-rank, integer milliseconds
    throughput_rps: int            # floor(completed / duration_s)
    total_requests: int
    duration_ms: int
    quantization_error_bounds: dict[str, str]
    aggregation_version: str


@dataclass
class StageResult:
    stage_index: int
    spec: dict[str, Any]           # StageSpec at execution time
    target: dict[str, Any]         # loopback target address at execution time
    params_version: int
    metrics: StageMetrics
    verdict: str
    saturation_reason: str | None
    triggered_metrics: dict[str, Any]
    observation: Observation

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_index": self.stage_index,
            "spec": self.spec,
            "target": self.target,
            "params_version": self.params_version,
            "metrics": asdict(self.metrics),
            "verdict": self.verdict,
            "saturation_reason": self.saturation_reason,
            "triggered_metrics": self.triggered_metrics,
            "observation": {
                "stage_index": self.observation.stage_index,
                "started_at_ms": self.observation.started_at_ms,
                "ended_at_ms": self.observation.ended_at_ms,
                "samples": [s.to_dict() for s in self.observation.samples],
                "server_confirmed_success": self.observation.server_confirmed_success,
                "server_confirmed_failure": self.observation.server_confirmed_failure,
                "clock_basis": self.observation.clock_basis,
                "truth_source": self.observation.truth_source,
            },
        }


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
