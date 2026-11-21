"""In-memory value objects for manifests, snapshots, and calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LayerSpec:
    name: str
    digest: str
    version: int
    default_init_ms: int
    tombstone: bool
    env: dict[str, str] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    referenced_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    runtime: str
    layer_refs: tuple[str, ...]
    layer_params: dict[str, dict[str, Any]]
    memory_mib: int
    max_concurrency: int
    warm_pool_target: int
    warm_ttl_ms: int
    init_base_ms: int


@dataclass(frozen=True)
class FunctionManifest:
    global_concurrency_budget: int
    functions: dict[str, FunctionSpec]


@dataclass(frozen=True)
class LayerManifest:
    layers: dict[str, LayerSpec]


@dataclass(frozen=True)
class Observation:
    function: str
    source: str
    absolute_init_ms: int
    observed_clock_ms: int
    warm_generation: int
    warm_hit: bool
    hit_watermark: int
    scale_num: int
    scale_den: int
    rounding: str
    conversion_error_bound_ms: float
    selection_reason: str


@dataclass(frozen=True)
class SnapshotMerge:
    observations: dict[str, Observation]
    migrations: tuple[dict[str, Any], ...]
    v1_payload: dict[str, Any]
    v2_payload: dict[str, Any]


@dataclass(frozen=True)
class CallSpec:
    seq: int
    function: str
    arrival_ms: int
    occupancy: int
    duration_ms: int
    wait_budget_ms: int
    cancel_after_ms: int | None = None


@dataclass(frozen=True)
class CallTrace:
    calls: tuple[CallSpec, ...]
