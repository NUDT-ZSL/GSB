from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .errors import BusinessError, ErrorCode
from .models import NodePool, NodeSpec, ServiceSpec


@dataclass
class PackingState:
    node_pool: NodePool
    gpu_free_mib: dict[tuple[str, int], int]
    node_instance_used: dict[str, int] = field(default_factory=dict)
    service_domain_used: dict[tuple[str, str], int] = field(default_factory=dict)
    compute_used: int = 0

    @classmethod
    def empty(cls, node_pool: NodePool) -> PackingState:
        gpu_free: dict[tuple[str, int], int] = {}
        node_used: dict[str, int] = {}
        for node in node_pool.nodes:
            node_used[node.name] = 0
            for gpu_index in range(node.gpu_count):
                gpu_free[(node.name, gpu_index)] = node.memory_mib_per_gpu
        return cls(
            node_pool=node_pool,
            gpu_free_mib=gpu_free,
            node_instance_used=node_used,
        )

    @classmethod
    def from_occupancies(
        cls,
        node_pool: NodePool,
        occupancies: Iterable[dict[str, Any]],
    ) -> PackingState:
        state = cls.empty(node_pool)
        for occupancy in occupancies:
            node_name = str(occupancy["node_name"])
            gpu_index = int(occupancy["gpu_index"])
            key = (node_name, gpu_index)
            if key not in state.gpu_free_mib:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "occupancy references an unknown node or GPU",
                    details={"node_name": node_name, "gpu_index": gpu_index},
                )
            memory_mib = int(occupancy["memory_mib"])
            state.gpu_free_mib[key] -= memory_mib
            if state.gpu_free_mib[key] < 0:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "persisted GPU memory occupancy exceeds card capacity",
                    details={
                        "node_name": node_name,
                        "gpu_index": gpu_index,
                        "overflow_mib": -state.gpu_free_mib[key],
                    },
                )
            state.node_instance_used[node_name] = (
                state.node_instance_used.get(node_name, 0) + 1
            )
            node = state.node_by_name(node_name)
            if (
                state.node_instance_used[node_name]
                > node.max_instances_per_node
            ):
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "persisted node occupancy exceeds the instance limit",
                    details={"node_name": node_name},
                )
            service_name = str(occupancy["service_name"])
            domain_key = (service_name, node.topology_domain)
            state.service_domain_used[domain_key] = (
                state.service_domain_used.get(domain_key, 0) + 1
            )
            domain_limit = node_pool.max_replicas_per_domain_per_service
            if (
                domain_limit is not None
                and state.service_domain_used[domain_key] > domain_limit
            ):
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "persisted topology occupancy exceeds the domain limit",
                    details={
                        "service_name": service_name,
                        "topology_domain": node.topology_domain,
                    },
                )
            state.compute_used += int(occupancy["compute_units"])
            if (
                node_pool.compute_units_capacity is not None
                and state.compute_used > node_pool.compute_units_capacity
            ):
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "persisted compute occupancy exceeds capacity",
                    details={
                        "compute_units_used": state.compute_used,
                        "compute_units_capacity": node_pool.compute_units_capacity,
                    },
                )
        return state

    def node_by_name(self, name: str) -> NodeSpec:
        for node in self.node_pool.nodes:
            if node.name == name:
                return node
        raise KeyError(name)

    def free_mib_by_node(self) -> dict[str, list[int]]:
        output: dict[str, list[int]] = {}
        for node in self.node_pool.nodes:
            output[node.name] = [
                max(0, self.gpu_free_mib[(node.name, gpu_index)])
                for gpu_index in range(node.gpu_count)
            ]
        return output


@dataclass(frozen=True)
class Placement:
    replica_id: str
    service_name: str
    node_name: str
    gpu_index: int
    memory_mib: int
    compute_units: int
    topology_domain: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "service_name": self.service_name,
            "node_name": self.node_name,
            "gpu_index": self.gpu_index,
            "memory_mib": self.memory_mib,
            "compute_units": self.compute_units,
            "topology_domain": self.topology_domain,
        }


@dataclass(frozen=True)
class PlacementResult:
    placement: Placement | None
    report: dict[str, Any]


def _candidate_report(
    node: NodeSpec,
    state: PackingState,
    *,
    first_constraint: str | None,
    detail: str,
) -> dict[str, Any]:
    return {
        "node_name": node.name,
        "topology_domain": node.topology_domain,
        "eligible": first_constraint is None,
        "first_constraint": first_constraint,
        "detail": detail,
        "gpu_free_mib": [
            max(0, state.gpu_free_mib[(node.name, gpu_index)])
            for gpu_index in range(node.gpu_count)
        ],
        "node_instances_used": state.node_instance_used.get(node.name, 0),
        "node_instance_limit": node.max_instances_per_node,
    }


def place_replica(
    state: PackingState,
    service: ServiceSpec,
    replica_id: str,
) -> PlacementResult:
    """Atomically place one whole replica on one GPU, or reject it completely."""

    if state.node_pool.compute_units_capacity is not None:
        compute_remaining = (
            state.node_pool.compute_units_capacity - state.compute_used
        )
        if compute_remaining < service.compute_units_per_replica:
            return PlacementResult(
                placement=None,
                report={
                    "replica_id": replica_id,
                    "service_name": service.name,
                    "first_constraint": "compute_budget",
                    "detail": "global compute units capacity is insufficient",
                    "compute_units_used": state.compute_used,
                    "compute_units_capacity": state.node_pool.compute_units_capacity,
                    "compute_units_required": service.compute_units_per_replica,
                    "candidates": [],
                    "gpu_free_mib": state.free_mib_by_node(),
                },
            )

    domain_limit = state.node_pool.max_replicas_per_domain_per_service
    evaluated: list[dict[str, Any]] = []
    eligible: list[tuple[tuple[int, str, int], NodeSpec, int]] = []

    for node in sorted(state.node_pool.nodes, key=lambda item: item.name):
        domain_count = state.service_domain_used.get(
            (service.name, node.topology_domain), 0
        )
        if (
            service.allowed_topology_domains
            and node.topology_domain not in service.allowed_topology_domains
        ):
            evaluated.append(
                _candidate_report(
                    node,
                    state,
                    first_constraint="topology_not_allowed",
                    detail="domain is not in service.allowed_topology_domains",
                )
            )
            continue
        if domain_limit is not None and domain_count >= domain_limit:
            evaluated.append(
                _candidate_report(
                    node,
                    state,
                    first_constraint="topology_domain_limit",
                    detail=(
                        f"service already has {domain_count} replicas in "
                        f"domain {node.topology_domain}"
                    ),
                )
            )
            continue
        used = state.node_instance_used.get(node.name, 0)
        if used >= node.max_instances_per_node:
            evaluated.append(
                _candidate_report(
                    node,
                    state,
                    first_constraint="node_instance_limit",
                    detail=f"node instances {used} reached limit {node.max_instances_per_node}",
                )
            )
            continue

        gpu_index = None
        for candidate_gpu in range(node.gpu_count):
            if (
                state.gpu_free_mib[(node.name, candidate_gpu)]
                >= service.memory_mib_per_replica
            ):
                gpu_index = candidate_gpu
                break
        if gpu_index is None:
            evaluated.append(
                _candidate_report(
                    node,
                    state,
                    first_constraint="gpu_memory",
                    detail=(
                        "no single GPU has enough free MiB for "
                        f"{service.memory_mib_per_replica}"
                    ),
                )
            )
            continue

        evaluated.append(
            _candidate_report(
                node,
                state,
                first_constraint=None,
                detail=f"feasible on gpu {gpu_index}",
            )
        )
        eligible.append(((domain_count, node.name, gpu_index), node, gpu_index))

    if not eligible:
        first = next(
            (
                candidate["first_constraint"]
                for candidate in evaluated
                if candidate["first_constraint"] is not None
            ),
            "no_node",
        )
        return PlacementResult(
            placement=None,
            report={
                "replica_id": replica_id,
                "service_name": service.name,
                "first_constraint": first,
                "detail": "no topology- and budget-feasible placement",
                "compute_units_used": state.compute_used,
                "compute_units_capacity": state.node_pool.compute_units_capacity,
                "candidates": evaluated,
                "gpu_free_mib": state.free_mib_by_node(),
            },
        )

    _, selected_node, selected_gpu = min(eligible, key=lambda item: item[0])
    state.gpu_free_mib[(selected_node.name, selected_gpu)] -= (
        service.memory_mib_per_replica
    )
    state.node_instance_used[selected_node.name] = (
        state.node_instance_used.get(selected_node.name, 0) + 1
    )
    domain_key = (service.name, selected_node.topology_domain)
    state.service_domain_used[domain_key] = (
        state.service_domain_used.get(domain_key, 0) + 1
    )
    state.compute_used += service.compute_units_per_replica

    for candidate in evaluated:
        candidate["selected"] = (
            candidate["node_name"] == selected_node.name
            and candidate["eligible"]
        )

    placement = Placement(
        replica_id=replica_id,
        service_name=service.name,
        node_name=selected_node.name,
        gpu_index=selected_gpu,
        memory_mib=service.memory_mib_per_replica,
        compute_units=service.compute_units_per_replica,
        topology_domain=selected_node.topology_domain,
    )
    return PlacementResult(
        placement=placement,
        report={
            "replica_id": replica_id,
            "service_name": service.name,
            "first_constraint": None,
            "detail": "placed atomically on one GPU",
            "selected": placement.to_dict(),
            "compute_units_used": state.compute_used,
            "compute_units_capacity": state.node_pool.compute_units_capacity,
            "candidates": evaluated,
            "gpu_free_mib": state.free_mib_by_node(),
        },
    )

