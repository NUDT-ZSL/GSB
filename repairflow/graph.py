"""The five-node deterministic repair DAG."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .errors import BusinessError, ErrorCode
from .hashing import sha256_json

NODE_MASK_PARSE = "mask_parse"
NODE_CONTEXT_SAMPLE = "boundary_context"
NODE_CONTENT_COMPLETION = "content_completion"
NODE_STYLE_TRANSFER = "style_transfer"
NODE_BLEND_OUTPUT = "blend_output"

NODE_ORDER: tuple[str, ...] = (
    NODE_MASK_PARSE,
    NODE_CONTEXT_SAMPLE,
    NODE_CONTENT_COMPLETION,
    NODE_STYLE_TRANSFER,
    NODE_BLEND_OUTPUT,
)

NODE_LABELS = {
    NODE_MASK_PARSE: "掩码区域解析",
    NODE_CONTEXT_SAMPLE: "边界上下文采样",
    NODE_CONTENT_COMPLETION: "区域内容确定性补全",
    NODE_STYLE_TRANSFER: "风格迁移着色",
    NODE_BLEND_OUTPUT: "边界融合与全图输出",
}

DEFAULT_EDGES: tuple[tuple[str, str], ...] = tuple(
    (NODE_ORDER[index], NODE_ORDER[index + 1]) for index in range(len(NODE_ORDER) - 1)
)


@dataclass(frozen=True)
class GraphSpec:
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "GraphSpec":
        raw = dict((config or {}).get("graph") or {})
        nodes = tuple(raw.get("nodes") or NODE_ORDER)
        raw_edges = raw.get("edges")
        edges = DEFAULT_EDGES if raw_edges is None else tuple(tuple(edge) for edge in raw_edges)

        if nodes != NODE_ORDER:
            raise BusinessError(
                ErrorCode.INVALID_GRAPH,
                "graph nodes must be the five deterministic pipeline nodes in order",
                field="graph.nodes",
                expected=list(NODE_ORDER),
                actual=list(nodes),
            )
        if edges != DEFAULT_EDGES:
            raise BusinessError(
                ErrorCode.INVALID_GRAPH,
                "graph edges must connect the five nodes as a validated chain",
                field="graph.edges",
                expected=[list(edge) for edge in DEFAULT_EDGES],
                actual=[list(edge) for edge in edges],
            )
        spec = cls(nodes=nodes, edges=edges)
        spec._validate_dag()
        return spec

    def _validate_dag(self) -> None:
        incoming = {node: 0 for node in self.nodes}
        outgoing: dict[str, list[str]] = {node: [] for node in self.nodes}
        for source, target in self.edges:
            if source not in incoming or target not in incoming:
                raise BusinessError(
                    ErrorCode.INVALID_GRAPH,
                    "graph edge references an unknown node",
                    field="graph.edges",
                    edge=[source, target],
                )
            incoming[target] += 1
            outgoing[source].append(target)

        queue = [node for node in self.nodes if incoming[node] == 0]
        visited: list[str] = []
        while queue:
            node = queue.pop(0)
            visited.append(node)
            for target in outgoing[node]:
                incoming[target] -= 1
                if incoming[target] == 0:
                    queue.append(target)
        if len(visited) != len(self.nodes):
            raise BusinessError(
                ErrorCode.INVALID_GRAPH,
                "graph contains a cycle",
                field="graph.edges",
            )

    def dependencies(self, node: str) -> tuple[str, ...]:
        return tuple(source for source, target in self.edges if target == node)

    def ancestors(self, node: str) -> tuple[str, ...]:
        found: list[str] = []

        def walk(current: str) -> None:
            for parent in self.dependencies(current):
                if parent not in found:
                    walk(parent)
                    found.append(parent)

        walk(node)
        return tuple(found)

    def topological_order(self, subset: Iterable[str] | None = None) -> tuple[str, ...]:
        selected = set(subset) if subset is not None else set(self.nodes)
        incoming = {node: 0 for node in selected}
        outgoing: dict[str, list[str]] = {node: [] for node in selected}
        for source, target in self.edges:
            if source in selected and target in selected:
                incoming[target] += 1
                outgoing[source].append(target)
        queue = [node for node in self.nodes if node in selected and incoming[node] == 0]
        order: list[str] = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for target in outgoing[node]:
                incoming[target] -= 1
                if incoming[target] == 0:
                    queue.append(target)
        return tuple(order)

    def signature(self) -> str:
        return sha256_json(
            {"nodes": list(self.nodes), "edges": [list(edge) for edge in self.edges]}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [
                {"id": node, "label": NODE_LABELS[node]} for node in self.nodes
            ],
            "edges": [list(edge) for edge in self.edges],
            "topological_order": list(self.topological_order()),
            "signature": self.signature(),
        }
