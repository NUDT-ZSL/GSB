from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class InstanceSpec:
    instance_id: str
    service: str
    priority: int
    requested_ephemeral_bytes: int
    required_layers: tuple[str, ...]
    deadline_ms: int
    node: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["required_layers"] = list(self.required_layers)
        return data


@dataclass(frozen=True)
class LayerSpec:
    digest: str
    bytes: int
    node: str
    ref_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NodeSpec:
    name: str
    available_ephemeral_bytes: int
    image_layer_bytes: int
    max_concurrent_cold_starts: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LedgerConfig:
    temp_storage_quota_limit_bytes: int
    layer_read_bandwidth_bytes_per_second: int
    round_logical_time_budget_ms: int
    fixed_scale: int
    error_upper_bound_budget: int
    logical_clock_step_ms: int
    comparison_tolerance_ticks: int
    clock_rollback_threshold_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InputBundle:
    instances: tuple[InstanceSpec, ...]
    layers: tuple[LayerSpec, ...]
    nodes: tuple[NodeSpec, ...]
    config: LedgerConfig

    @property
    def instances_by_id(self) -> dict[str, InstanceSpec]:
        return {item.instance_id: item for item in self.instances}

    @property
    def layers_by_digest(self) -> dict[str, LayerSpec]:
        return {item.digest: item for item in self.layers}

    @property
    def nodes_by_name(self) -> dict[str, NodeSpec]:
        return {item.name: item for item in self.nodes}

    def to_dict(self) -> dict[str, Any]:
        return {
            "instances": [item.to_dict() for item in self.instances],
            "layers": [item.to_dict() for item in self.layers],
            "nodes": [item.to_dict() for item in self.nodes],
            "config": self.config.to_dict(),
        }

