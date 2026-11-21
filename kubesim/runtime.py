from __future__ import annotations

from dataclasses import dataclass

from .models import Resources, Taint


@dataclass(frozen=True)
class NodeState:
    name: str
    capacity: Resources
    remaining: Resources
    resource_version: int
    labels: dict[str, str]
    taints: tuple[Taint, ...]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "capacity": self.capacity.to_dict(),
            "remaining": self.remaining.to_dict(),
            "resource_version": self.resource_version,
            "labels": dict(self.labels),
            "taints": [taint.to_dict() for taint in self.taints],
        }


@dataclass(frozen=True)
class BoundPod:
    name: str
    node_name: str
    labels: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "node_name": self.node_name,
            "labels": dict(self.labels),
        }


@dataclass(frozen=True)
class ClusterState:
    nodes: tuple[NodeState, ...]
    bound_pods: tuple[BoundPod, ...]

    @property
    def nodes_by_name(self) -> dict[str, NodeState]:
        return {node.name: node for node in self.nodes}

    def to_dict(self) -> dict:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "bound_pods": [pod.to_dict() for pod in self.bound_pods],
        }
