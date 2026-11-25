"""Canonical in-memory data model for fixtures and scheduler plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LayerSpec:
    name: str
    params: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "params": self.params}


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    layers: tuple[LayerSpec, ...]
    flops_per_parameter_per_token: float
    memory_base_mib: float
    kv_mib_per_token: float
    activation_mib_per_token: float
    batch_overhead_ms: float

    @property
    def total_params(self) -> int:
        return sum(layer.params for layer in self.layers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "layers": [layer.to_dict() for layer in self.layers],
            "flops_per_parameter_per_token": self.flops_per_parameter_per_token,
            "memory_base_mib": self.memory_base_mib,
            "kv_mib_per_token": self.kv_mib_per_token,
            "activation_mib_per_token": self.activation_mib_per_token,
            "batch_overhead_ms": self.batch_overhead_ms,
        }


@dataclass(frozen=True)
class ReplicaSpec:
    replica_id: str
    compute_capacity: float
    memory_limit_mib: float
    window_ms: int
    max_batch_size: int
    max_sequence_length: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "compute_capacity": self.compute_capacity,
            "memory_limit_mib": self.memory_limit_mib,
            "window_ms": self.window_ms,
            "max_batch_size": self.max_batch_size,
            "max_sequence_length": self.max_sequence_length,
        }


@dataclass(frozen=True)
class RequestSpec:
    request_id: int
    arrival_ms: int
    priority: int
    input_tokens: int
    max_output_tokens: int
    deadline_ms: int

    @property
    def sequence_tokens(self) -> int:
        return self.input_tokens + self.max_output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "arrival_ms": self.arrival_ms,
            "priority": self.priority,
            "input_tokens": self.input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "deadline_ms": self.deadline_ms,
        }


@dataclass(frozen=True)
class AccuracySpec:
    compute_scale: float
    memory_scale: float
    latency_scale: float
    float_tolerance: float
    error_budget: float
    rounding_mode: str

    def scale_for(self, metric: str) -> float:
        if metric == "compute":
            return self.compute_scale
        if metric == "memory":
            return self.memory_scale
        if metric == "latency":
            return self.latency_scale
        raise KeyError(metric)

    def to_dict(self) -> dict[str, Any]:
        return {
            "compute_scale": self.compute_scale,
            "memory_scale": self.memory_scale,
            "latency_scale": self.latency_scale,
            "float_tolerance": self.float_tolerance,
            "error_budget": self.error_budget,
            "rounding_mode": self.rounding_mode,
        }


@dataclass(frozen=True)
class InjectionSpec:
    injection_id: str
    replica_id: str | None
    window_index: int
    phase: str
    batch_index: int | None
    signal_name: str
    enabled: bool
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "injection_id": self.injection_id,
            "replica_id": self.replica_id,
            "window_index": self.window_index,
            "phase": self.phase,
            "batch_index": self.batch_index,
            "signal": self.signal_name,
            "enabled": self.enabled,
            "sequence": self.sequence,
        }


@dataclass(frozen=True)
class Fixture:
    schema_version: int
    model: ModelSpec
    replicas: tuple[ReplicaSpec, ...]
    requests: tuple[RequestSpec, ...]
    accuracy: AccuracySpec
    injections: tuple[InjectionSpec, ...]
    canonical: dict[str, Any]
    content_hash: str
    source: str

    def replica_map(self) -> dict[str, ReplicaSpec]:
        return {replica.replica_id: replica for replica in self.replicas}

    def request_map(self) -> dict[int, RequestSpec]:
        return {request.request_id: request for request in self.requests}


@dataclass(frozen=True)
class Route:
    request_id: int
    replica_id: str
    window_index: int
    window_start_ms: int
    window_end_ms: int
    version: int
    precision_events: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "replica_id": self.replica_id,
            "window_index": self.window_index,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "version": self.version,
            "precision_events": list(self.precision_events),
        }
