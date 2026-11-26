from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class InstancePhase(str, Enum):
    PENDING_WARMUP = "pending_warmup"
    WARMING = "warming"
    READY = "ready"
    BUSY = "busy"
    RECYCLING = "recycling"
    ABANDONED = "abandoned"
    FAILED = "warmup_failed"
    RECYCLED = "recycled"


PHASE_LABELS_CN = {
    InstancePhase.PENDING_WARMUP.value: "待预热",
    InstancePhase.WARMING.value: "预热中",
    InstancePhase.READY.value: "就绪",
    InstancePhase.BUSY.value: "忙碌",
    InstancePhase.RECYCLING.value: "回收中",
    InstancePhase.ABANDONED.value: "已放弃",
    InstancePhase.FAILED.value: "预热失败",
    InstancePhase.RECYCLED.value: "已回收",
}


class WarmupOutcome(str, Enum):
    STARTED = "started"
    NOT_STARTED_IN_BUDGET = "not_started_in_budget"
    WARMUP_FAILED = "warmup_failed"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    cold_start_ms: int
    concurrency_limit: int
    warmup_window_ms: int = 60_000
    container_spec: str = "default"
    priority: int = 100
    dependencies: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class RequestRecord:
    seq: int
    function_name: str
    logical_clock_ms: int
    priority: int = 100
    expected_latency_ms: int = 1
    call_status: str = "submitted"
    duration_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def occupancy_ms(self) -> int:
        if self.duration_ms is not None:
            return self.duration_ms
        return self.expected_latency_ms


@dataclass(frozen=True)
class PolicyRecord:
    key: str
    format_generation: str
    generation: int
    declared_clock_ms: int
    effective_clock_ms: int
    original_time_kind: str
    original_time_value: int
    source: str
    source_version: str = ""
    function_name: str | None = None
    tombstone: bool = False
    pool: dict[str, Any] = field(default_factory=dict)
    priority: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class WarmupCandidate:
    candidate_id: str
    function_name: str
    request_seq: int
    function_priority: int
    request_priority: int
    cold_start_ms: int
    order_index: int = 0


@dataclass
class WarmupDecision:
    candidate_id: str
    function_name: str
    request_seq: int
    outcome: str
    reason: str
    round_id: str
    clock_ms: int
    cold_start_ms: int
    budget_before_ms: int
    budget_after_ms: int
    active_warm_before: int
    active_warm_after: int
    instance_id: str | None = None
    charge_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return {k: jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value
