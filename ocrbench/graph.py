"""Computation-graph topology and deterministic node execution framework."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence, Tuple

from .config import GRAPH_NODES, PipelineConfig
from .errors import ErrorCode, make_error


def validate_topology(
    nodes: Sequence[str], edges: Sequence[Tuple[str, str]]
) -> List[str]:
    """Validate the DAG and return a deterministic topological ordering.

    Raises ``E_GRAPH_CYCLE`` when a cycle is present and ``E_GRAPH_CONFIG``
    when an edge references an unknown node.
    """

    node_set = set(nodes)
    indegree = {node: 0 for node in nodes}
    adjacency: Dict[str, List[str]] = {node: [] for node in nodes}
    for src, dst in edges:
        if src not in node_set or dst not in node_set:
            raise make_error(
                ErrorCode.GRAPH_CONFIG,
                "edge references unknown node",
                stage="topology",
                edge=[src, dst],
                known_nodes=sorted(node_set),
            )
        adjacency[src].append(dst)
        indegree[dst] += 1

    # Deterministic Kahn process using sorted ready set.
    ready = sorted(node for node, deg in indegree.items() if deg == 0)
    order: List[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in sorted(adjacency[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort()

    if len(order) != len(nodes):
        remaining = sorted(set(nodes) - set(order))
        raise make_error(
            ErrorCode.GRAPH_CYCLE,
            "computation graph contains a cycle",
            stage="topology",
            remaining_nodes=remaining,
        )
    return order


@dataclass
class NodeResult:
    """The outcome of executing one graph node for one page."""

    node: str
    key: str
    key_details: Dict[str, object]
    payload: Dict[str, object]
    content_hash: str
    reused: bool


class ExecutionPlan:
    """Resolved node ordering for a configuration."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.order = validate_topology(GRAPH_NODES, config.edges)

    def upstream_of(self, node: str) -> List[str]:
        return self.config.upstream(node)

    def suffix_from(self, entry_nodes: Mapping[str, bool]) -> List[str]:
        """Return all nodes reachable downstream from the marked entries."""

        affected = set(node for node, mark in entry_nodes.items() if mark)
        changed = True
        while changed:
            changed = False
            for src, dst in self.config.edges:
                if src in affected and dst not in affected:
                    affected.add(dst)
                    changed = True
        return [node for node in self.order if node in affected]
