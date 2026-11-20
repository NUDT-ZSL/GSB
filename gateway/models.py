"""Data model for services, nodes, trace and engine configuration."""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    model_version: str
    input_tokens: int
    output_tokens: int
    compute_units: int
    vram_mib: int
    min_replicas: int
    max_replicas: int
    batch_window_ms: int
    cold_start_ms: int
    topology_domain: str  # "any" or a concrete domain name


@dataclass(frozen=True)
class NodeSpec:
    name: str
    gpu_cards: int
    vram_per_card_mib: int
    max_instances: int
    topology_domain: str


@dataclass(frozen=True)
class FaultSpec:
    kind: str  # conn_refused | budget_exceeded | digest_mismatch
    every: int  # fault hits requests whose seq % every == 0 (first attempt)
    transient: bool  # True: only first attempt fails; False: all attempts


@dataclass(frozen=True)
class Arrival:
    service: str
    arrival_rate: float
    avg_seq_len: float
    expected_concurrency: float
    token_budget: int
    fault: Optional[FaultSpec]


@dataclass(frozen=True)
class TraceWindow:
    index: int
    start_ms: int
    window_ms: int
    arrivals: Tuple[Arrival, ...]


@dataclass(frozen=True)
class EngineConfig:
    switch_load_threshold: float
    round_time_budget_ms: int
    per_request_budget_ms: int
    clock_rollback_threshold_ms: int
    max_retries: int
    retry_backoff_base_ms: int
    per_service_concurrency_cap: int
    batch_overhead_ms: int


@dataclass(frozen=True)
class Trace:
    config: EngineConfig
    windows: Tuple[TraceWindow, ...]


# Request lifecycle stages (persisted).
STAGE_PENDING = "pending"
STAGE_QUEUED = "queued"
STAGE_BATCHING = "batching"
STAGE_DISPATCHED = "dispatched"
STAGE_FAILED = "failed"

# Request outcomes (persisted).
OUTCOME_SUCCESS = "success"
OUTCOME_NOT_STARTED = "not_started"  # budget remained but request never started
OUTCOME_FAILED_AFTER_START = "failed_after_start"  # started, then upstream failed
OUTCOME_ABANDONED = "abandoned"  # actively abandoned (token budget exhausted)

MODE_PER_REQUEST = "per_request"
MODE_WINDOW_BATCH = "window_batch"
