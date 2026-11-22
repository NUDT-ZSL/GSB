"""Validated immutable domain objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LayerSpec:
    name: str
    parameters: int


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    layers: tuple[LayerSpec, ...]
    input_tokens: int
    output_tokens: int
    bytes_per_parameter: int
    activation_bytes_per_token: int
    latency_ms_per_compute_unit: float

    @property
    def total_parameters(self) -> int:
        return sum(layer.parameters for layer in self.layers)

    @property
    def weights_mib(self) -> float:
        return self.total_parameters * self.bytes_per_parameter / (1024.0 * 1024.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "layers": [
                {"name": layer.name, "parameters": layer.parameters}
                for layer in self.layers
            ],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "bytes_per_parameter": self.bytes_per_parameter,
            "activation_bytes_per_token": self.activation_bytes_per_token,
            "latency_ms_per_compute_unit": self.latency_ms_per_compute_unit,
            "total_parameters": self.total_parameters,
            "weights_mib": self.weights_mib,
        }


@dataclass(frozen=True)
class ReplicaSpec:
    replica_id: str
    compute_units: float
    memory_mib: int
    batch_window_ms: int
    max_batch_size: int
    max_sequence_length: int
    max_concurrent_batches: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "compute_units": self.compute_units,
            "memory_mib": self.memory_mib,
            "batch_window_ms": self.batch_window_ms,
            "max_batch_size": self.max_batch_size,
            "max_sequence_length": self.max_sequence_length,
            "max_concurrent_batches": self.max_concurrent_batches,
        }


@dataclass(frozen=True)
class ReplicaVersion:
    version: int
    effective_at_ms: int
    replicas: tuple[ReplicaSpec, ...]
    fingerprint: str

    def by_id(self) -> dict[str, ReplicaSpec]:
        return {replica.replica_id: replica for replica in self.replicas}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "effective_at_ms": self.effective_at_ms,
            "fingerprint": self.fingerprint,
            "replicas": [replica.to_dict() for replica in self.replicas],
        }


@dataclass(frozen=True)
class RequestSpec:
    seq: int
    arrival_ms: int
    priority: int
    input_tokens: int
    max_output_tokens: int
    deadline_ms: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.max_output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "arrival_ms": self.arrival_ms,
            "priority": self.priority,
            "input_tokens": self.input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "deadline_ms": self.deadline_ms,
        }


@dataclass(frozen=True)
class PrecisionConfig:
    compute_scale: int
    memory_scale: int
    latency_scale: int
    tolerance: float
    rounding: str = "half_up"

    def to_dict(self) -> dict[str, Any]:
        return {
            "compute_scale": self.compute_scale,
            "memory_scale": self.memory_scale,
            "latency_scale": self.latency_scale,
            "tolerance": self.tolerance,
            "rounding": self.rounding,
        }


@dataclass(frozen=True)
class SchedulerConfig:
    logical_time_budget_ms: int
    max_concurrent_batches: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_time_budget_ms": self.logical_time_budget_ms,
            "max_concurrent_batches": self.max_concurrent_batches,
        }


@dataclass(frozen=True)
class ExecutionConfig:
    mode: str
    fail_batches: tuple[str, ...]
    latency_overhead_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fail_batches": list(self.fail_batches),
            "latency_overhead_ms": self.latency_overhead_ms,
        }


@dataclass(frozen=True)
class Bundle:
    model: ModelSpec
    replica_versions: tuple[ReplicaVersion, ...]
    requests: tuple[RequestSpec, ...]
    precision: PrecisionConfig
    scheduler: SchedulerConfig
    execution: ExecutionConfig
    digest: str
    raw: dict[str, Any]

    @property
    def current_version(self) -> int:
        return max(version.version for version in self.replica_versions)

    def version_map(self) -> dict[int, ReplicaVersion]:
        return {version.version: version for version in self.replica_versions}
