from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import sha256_json
from .errors import PipelineError

CANONICAL_NODE_TYPES = (
    "image_encoder",
    "text_encoder",
    "align",
    "index_build",
    "retrieve",
)


@dataclass(frozen=True)
class GraphSpec:
    nodes: dict[str, dict[str, Any]]
    edges: tuple[tuple[str, str], ...]
    entrypoint: str

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "GraphSpec":
        graph = config.get("graph")
        if not isinstance(graph, dict):
            raise PipelineError(
                "E_CONFIG_INVALID",
                "graph must be an object",
                stage="config",
                field="graph",
            )
        raw_nodes = graph.get("nodes")
        raw_edges = graph.get("edges")
        if not isinstance(raw_nodes, dict) or not isinstance(raw_edges, list):
            raise PipelineError(
                "E_CONFIG_INVALID",
                "graph.nodes must be an object and graph.edges must be a list",
                stage="config",
                field="graph",
            )
        nodes: dict[str, dict[str, Any]] = {}
        for name in sorted(raw_nodes):
            node = raw_nodes[name]
            if not isinstance(node, dict):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"graph node {name} must be an object",
                    stage="config",
                    field=f"graph.nodes.{name}",
                )
            node_type = node.get("type")
            if node_type not in CANONICAL_NODE_TYPES:
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"Unknown graph node type: {node_type}",
                    stage="config",
                    field=f"graph.nodes.{name}.type",
                )
            nodes[name] = dict(node)
        edges: list[tuple[str, str]] = []
        for edge in raw_edges:
            if (
                not isinstance(edge, list)
                or len(edge) != 2
                or not all(isinstance(x, str) for x in edge)
            ):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    "Each graph edge must be a two-element string list",
                    stage="config",
                    field="graph.edges",
                )
            source, target = edge
            if source not in nodes or target not in nodes:
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"Edge references unknown node: {source} -> {target}",
                    stage="config",
                    field="graph.edges",
                )
            edges.append((source, target))
        entrypoint = graph.get("entrypoint")
        if entrypoint not in nodes:
            raise PipelineError(
                "E_CONFIG_INVALID",
                "graph.entrypoint must name an existing node",
                stage="config",
                field="graph.entrypoint",
            )
        spec = cls(nodes=nodes, edges=tuple(sorted(edges)), entrypoint=entrypoint)
        spec.topological_order()
        return spec

    def topological_order(self) -> list[str]:
        indegree = {name: 0 for name in self.nodes}
        outgoing: dict[str, list[str]] = {name: [] for name in self.nodes}
        for source, target in self.edges:
            indegree[target] += 1
            outgoing[source].append(target)
        for name in outgoing:
            outgoing[name].sort()
        ready = [name for name, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            name = heapq.heappop(ready)
            order.append(name)
            for target in outgoing[name]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    heapq.heappush(ready, target)
        if len(order) != len(self.nodes):
            raise PipelineError(
                "E_CONFIG_INVALID",
                "Compute graph contains a cycle",
                stage="graph",
                field="graph.edges",
                details={"topological_prefix": order},
            )
        return order

    def type_positions(self) -> dict[str, int]:
        order = self.topological_order()
        result: dict[str, int] = {}
        for index, name in enumerate(order):
            node_type = self.nodes[name]["type"]
            result[node_type] = index
        return result

    def digest(self) -> str:
        return sha256_json(
            {
                "nodes": self.nodes,
                "edges": [list(edge) for edge in self.edges],
                "entrypoint": self.entrypoint,
            }
        )

    def instances(self, pair_ids: Iterable[str], block_ids: Iterable[int]) -> list[str]:
        pairs = sorted(pair_ids)
        blocks = sorted(int(x) for x in block_ids)
        result: list[str] = []
        for name in self.topological_order():
            node_type = self.nodes[name]["type"]
            if node_type in {"image_encoder", "text_encoder", "align", "retrieve"}:
                result.extend(f"{name}:{pair_id}" for pair_id in pairs)
            elif node_type == "index_build":
                result.extend(f"{name}:block-{block_id}" for block_id in blocks)
        return result


def instance_id(node_name: str, suffix: str | int) -> str:
    return f"{node_name}:{suffix}"


def node_type_for_instance(graph: GraphSpec, instance: str) -> str:
    node_name = instance.split(":", 1)[0]
    try:
        return graph.nodes[node_name]["type"]
    except KeyError as exc:
        raise PipelineError(
            "E_VERIFY_FAIL",
            f"Unknown node instance in snapshot: {instance}",
            stage="verify",
            field="nodes",
            details={"location": instance, "category": "unknown_node_instance"},
        ) from exc

