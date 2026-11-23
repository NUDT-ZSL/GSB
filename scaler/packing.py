from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import NodeSpec, ServiceSpec


@dataclass(frozen=True)
class Placement:
    node: str
    gpu_index: int
    memory_mib: int
    topology_domain: str


@dataclass
class PlacementDecision:
    replica_id: str
    service: str
    placement: Placement | None
    idempotent: bool
    eliminated: list[dict[str, Any]]
    first_constraint: str | None
    node_ledger_after: dict[str, dict[str, Any]]
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "service": self.service,
            "placement": (
                {
                    "node": self.placement.node,
                    "gpu_index": self.placement.gpu_index,
                    "memory_mib": self.placement.memory_mib,
                    "topology_domain": self.placement.topology_domain,
                }
                if self.placement
                else None
            ),
            "idempotent": self.idempotent,
            "eliminated": self.eliminated,
            "first_constraint": self.first_constraint,
            "node_ledger_after": self.node_ledger_after,
            "evidence": self.evidence,
        }


def choose_placement(
    *,
    service: ServiceSpec,
    replica_id: str,
    node_specs: dict[str, NodeSpec],
    node_ledger: dict[str, dict[str, Any]],
    existing: dict[str, Any] | None = None,
) -> PlacementDecision:
    if existing is not None:
        placement = Placement(
            node=existing["node"],
            gpu_index=existing["gpu_index"],
            memory_mib=existing["memory_mib"],
            topology_domain=existing["topology_domain"],
        )
        return PlacementDecision(
            replica_id=replica_id,
            service=service.name,
            placement=placement,
            idempotent=True,
            eliminated=[],
            first_constraint=None,
            node_ledger_after=_ledger_view(node_specs, node_ledger),
            evidence={"reason": "placement already exists for this replica and round"},
        )

    eliminated: list[dict[str, Any]] = []
    first_constraint: str | None = None
    for node_name in sorted(node_specs):
        node = node_specs[node_name]
        ledger = node_ledger[node_name]
        if node.topology_domain not in service.topology_domains:
            reason = "TOPOLOGY_DOMAIN_MISMATCH"
            eliminated.append(
                {
                    "node": node_name,
                    "reason": reason,
                    "node_domain": node.topology_domain,
                    "allowed_domains": list(service.topology_domains),
                }
            )
            first_constraint = first_constraint or reason
            continue
        if ledger["used_instances"] >= node.max_instances:
            reason = "NODE_CONCURRENCY_EXHAUSTED"
            eliminated.append(
                {
                    "node": node_name,
                    "reason": reason,
                    "used_instances": ledger["used_instances"],
                    "max_instances": node.max_instances,
                }
            )
            first_constraint = first_constraint or reason
            continue
        gpu_index = None
        for index, remaining in enumerate(ledger["gpu_remaining_mib"]):
            if remaining >= service.replica_memory_mib:
                gpu_index = index
                break
        if gpu_index is None:
            reason = "GPU_MEMORY_EXHAUSTED"
            eliminated.append(
                {
                    "node": node_name,
                    "reason": reason,
                    "gpu_remaining_mib": list(ledger["gpu_remaining_mib"]),
                    "required_mib": service.replica_memory_mib,
                }
            )
            first_constraint = first_constraint or reason
            continue

        placement = Placement(
            node=node_name,
            gpu_index=gpu_index,
            memory_mib=service.replica_memory_mib,
            topology_domain=node.topology_domain,
        )
        ledger["gpu_remaining_mib"][gpu_index] -= service.replica_memory_mib
        ledger["used_instances"] += 1
        eliminated.append(
            {
                "node": node_name,
                "reason": "SELECTED",
                "gpu_index": gpu_index,
                "memory_before_mib": ledger["gpu_remaining_mib"][gpu_index]
                + service.replica_memory_mib,
                "memory_after_mib": ledger["gpu_remaining_mib"][gpu_index],
                "used_instances_before": ledger["used_instances"] - 1,
                "used_instances_after": ledger["used_instances"],
            }
        )
        return PlacementDecision(
            replica_id=replica_id,
            service=service.name,
            placement=placement,
            idempotent=False,
            eliminated=eliminated,
            first_constraint=first_constraint,
            node_ledger_after=_ledger_view(node_specs, node_ledger),
            evidence={
                "selection_order": "node name ascending, then lowest GPU index with capacity",
                "constraints_checked": [
                    "topology_domain",
                    "node_concurrency",
                    "per_gpu_memory",
                ],
                "atomic": True,
            },
        )

    return PlacementDecision(
        replica_id=replica_id,
        service=service.name,
        placement=None,
        idempotent=False,
        eliminated=eliminated,
        first_constraint=first_constraint or "NO_NODES",
        node_ledger_after=_ledger_view(node_specs, node_ledger),
        evidence={
            "selection_order": "node name ascending, then lowest GPU index with capacity",
            "constraints_checked": [
                "topology_domain",
                "node_concurrency",
                "per_gpu_memory",
            ],
            "atomic": True,
            "partial_placement": False,
        },
    )


def _ledger_view(
    node_specs: dict[str, NodeSpec], node_ledger: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "gpu_remaining_mib": list(node_ledger[name]["gpu_remaining_mib"]),
            "gpu_capacity_mib": [
                node_specs[name].gpu_memory_mib
                for _ in range(node_specs[name].gpu_count)
            ],
            "used_instances": node_ledger[name]["used_instances"],
            "max_instances": node_specs[name].max_instances,
        }
        for name in sorted(node_ledger)
    }


def make_initial_ledger(node_specs: dict[str, NodeSpec]) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "gpu_remaining_mib": [spec.gpu_memory_mib for _ in range(spec.gpu_count)],
            "used_instances": 0,
        }
        for name, spec in sorted(node_specs.items())
    }
