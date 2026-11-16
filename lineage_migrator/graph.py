"""Mutable in-memory lineage graph and deterministic helpers."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .errors import BusinessCode, MigrationError
from .models import Edge, Field


@dataclass
class ChangeRecord:
    """One graph mutation, attributed to the rule that caused it."""

    rule_id: str
    rule_type: str
    version: int
    action: str
    detail: dict


@dataclass
class GraphState:
    """Working copy of the lineage graph for a single version attempt."""

    fields: dict[str, Field] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    changes: list[ChangeRecord] = field(default_factory=list)
    edge_seq: int = 0

    @classmethod
    def from_snapshot(cls, fields: list[Field], edges: list[Edge]) -> "GraphState":
        state = cls()
        state.fields = {item.ref: item for item in fields}
        state.edges = {item.edge_id: item for item in edges}
        state.edge_seq = len(edges)
        return state

    def clone(self) -> "GraphState":
        return copy.deepcopy(self)

    def require_field(self, ref: str, rule_id: str) -> Field:
        found = self.fields.get(ref)
        if found is None:
            raise MigrationError(
                BusinessCode.E_TARGET_MISSING,
                f"field {ref} does not exist",
                rule_id=rule_id,
            )
        return found

    def require_live_field(self, ref: str, rule_id: str) -> Field:
        found = self.require_field(ref, rule_id)
        if found.deprecated:
            raise MigrationError(
                BusinessCode.E_STALE_SOURCE,
                f"field {ref} is deprecated and cannot be referenced",
                rule_id=rule_id,
            )
        return found

    def next_edge_id(self, version: int) -> str:
        self.edge_seq += 1
        return f"e_v{version}_{self.edge_seq:04d}"

    def record(self, rule_id: str, rule_type: str, version: int, action: str, detail: dict) -> None:
        self.changes.append(
            ChangeRecord(
                rule_id=rule_id,
                rule_type=rule_type,
                version=version,
                action=action,
                detail=detail,
            )
        )

    def ordered_fields(self) -> list[Field]:
        return [self.fields[ref] for ref in sorted(self.fields)]

    def ordered_edges(self) -> list[Edge]:
        return [self.edges[eid] for eid in sorted(self.edges)]


def detect_cycle(edges: list[Edge]) -> list[str] | None:
    """Return an example cycle of field refs, or None when the graph is a DAG.

    Iterative DFS with white/grey/black coloring; neighbor order is sorted so
    the reported cycle is deterministic across runs.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.source_ref, []).append(edge.target_ref)
    for refs in adjacency.values():
        refs.sort()
    white, grey, black = 0, 1, 2
    color: dict[str, int] = {}
    stack: list[tuple[str, int]] = []
    for start in sorted(set(adjacency) | {target for refs in adjacency.values() for target in refs}):
        if color.get(start, white) != white:
            continue
        color[start] = grey
        stack.append((start, 0))
        path = [start]
        while stack:
            node, index = stack[-1]
            neighbors = adjacency.get(node, [])
            if index < len(neighbors):
                neighbor = neighbors[index]
                stack[-1] = (node, index + 1)
                if color.get(neighbor, white) == white:
                    color[neighbor] = grey
                    stack.append((neighbor, 0))
                    path.append(neighbor)
                elif color.get(neighbor) == grey:
                    cycle_start = path.index(neighbor)
                    return path[cycle_start:] + [neighbor]
            else:
                color[node] = black
                stack.pop()
                path.pop()
    return None


def weighted_bucket(edge_id: str, weights: tuple[int, ...]) -> int:
    """Deterministically map an edge to a weight bucket.

    The edge id is hashed with the stable blake2b digest and reduced modulo
    the total weight, then cumulative weights pick the target bucket. The
    mapping never depends on dict ordering or process memory layout.
    """
    import hashlib

    total = sum(weights)
    digest = int.from_bytes(hashlib.blake2b(edge_id.encode("utf-8"), digest_size=8).digest(), "big")
    point = digest % total
    cumulative = 0
    for index, weight in enumerate(weights):
        cumulative += weight
        if point < cumulative:
            return index
    return len(weights) - 1
