"""Dependency graph, cycle detection and reverse invalidation closure."""

from __future__ import annotations

import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .codes import E_DEP_CYCLE

sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))


@dataclass
class DependencyGraph:
    outgoing: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    incoming: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    edges: list[dict] = field(default_factory=list)

    @classmethod
    def from_edges(cls, edges: list[dict]):
        graph = cls()
        seen = set()
        for edge in edges:
            dependent = edge["dependent_key"]
            dependency = edge["dependency_key"]
            edge_id = (dependent, dependency)
            if edge_id in seen:
                continue
            seen.add(edge_id)
            graph.outgoing[dependent].append(dependency)
            graph.incoming[dependency].append(dependent)
            graph.edges.append(dict(edge))
        for values in graph.outgoing.values():
            values.sort()
        for values in graph.incoming.values():
            values.sort()
        graph.edges.sort(key=lambda item: item["edge_id"])
        return graph

    @property
    def nodes(self) -> set[str]:
        nodes = set(self.outgoing) | set(self.incoming)
        return nodes

    def find_cycles(self) -> list[dict]:
        """Return deterministic E_DEP_CYCLE records for cyclic SCCs."""

        index = 0
        indices: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        sccs: list[list[str]] = []

        def strongconnect(node: str) -> None:
            nonlocal index
            indices[node] = index
            lowlink[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)

            for neighbor in sorted(self.outgoing.get(node, [])):
                if neighbor not in indices:
                    strongconnect(neighbor)
                    lowlink[node] = min(lowlink[node], lowlink[neighbor])
                elif neighbor in on_stack:
                    lowlink[node] = min(lowlink[node], indices[neighbor])

            if lowlink[node] == indices[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.remove(member)
                    component.append(member)
                    if member == node:
                        break
                sccs.append(sorted(component))

        for node in sorted(self.nodes):
            if node not in indices:
                strongconnect(node)

        cycles = []
        for component in sorted(sccs, key=lambda members: members[0]):
            if len(component) == 1:
                node = component[0]
                if node in self.outgoing.get(node, []):
                    cycles.append(
                        {
                            "code": E_DEP_CYCLE,
                            "artifact_key": node,
                            "cycle": [node, node],
                            "members": [node],
                            "length": 1,
                            "message": "self-referential dependency",
                        }
                    )
                continue
            cycle_path = _extract_cycle(component, self.outgoing)
            if cycle_path:
                cycles.append(
                    {
                        "code": E_DEP_CYCLE,
                        "artifact_key": cycle_path[0],
                        "cycle": cycle_path,
                            "members": component,
                        "length": len(cycle_path) - 1,
                        "message": "dependency cycle detected",
                    }
                )
        return cycles


def _extract_cycle(component: list[str], outgoing: dict[str, list[str]]) -> list[str]:
    members = set(component)
    start = component[0]
    path = [start]
    visited_edges = set()

    def dfs(node: str) -> list[str] | None:
        for neighbor in sorted(outgoing.get(node, [])):
            if neighbor not in members:
                continue
            edge = (node, neighbor)
            if edge in visited_edges:
                continue
            visited_edges.add(edge)
            if neighbor == start and len(path) >= 1:
                return path + [start]
            if neighbor not in path:
                path.append(neighbor)
                found = dfs(neighbor)
                if found is not None:
                    return found
                path.pop()
        return None

    found = dfs(start)
    if found:
        return found
    # Defensive fallback: every non-singleton SCC has a cycle but keep a stable
    # representation even if the bounded DFS extraction misses an edge order.
    return component + [component[0]]


def closure_for_source(source_key: str, graph: DependencyGraph) -> dict[str, dict]:
    """Return invalidation entries keyed by affected artifact.

    Level zero is the revoked/changed source.  Reverse graph traversal moves
    from a dependency to the artifacts that depend on it.
    """

    entries: dict[str, dict] = {
        source_key: {
            "source_key": source_key,
            "level": 0,
            "chain": [source_key],
            "source_to_dependent_path": [source_key],
            "reason": "invalidation_source",
        }
    }
    queue = deque([(source_key, [source_key], 0)])
    visited = {source_key}
    while queue:
        node, source_to_node, level = queue.popleft()
        for dependent in graph.incoming.get(node, []):
            if dependent in visited:
                continue
            visited.add(dependent)
            source_to_dependent = source_to_node + [dependent]
            entries[dependent] = {
                "source_key": source_key,
                "level": level + 1,
                "chain": list(reversed(source_to_dependent)),
                "source_to_dependent_path": source_to_dependent,
                "reason": "reverse_dependency",
            }
            queue.append((dependent, source_to_dependent, level + 1))
    return entries


def build_invalidation_index(
    source_reasons: dict[str, str], graph: DependencyGraph
) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
    index: dict[str, list[dict]] = defaultdict(list)
    closure_sets: dict[str, list[str]] = {}
    for source_key in sorted(source_reasons):
        entries = closure_for_source(source_key, graph)
        closure_sets[source_key] = sorted(entries)
        for affected_key in sorted(entries):
            entry = dict(entries[affected_key])
            entry["reason"] = source_reasons[source_key] if affected_key == source_key else "reverse_dependency"
            index[affected_key].append(entry)
    for key in index:
        index[key].sort(key=lambda item: (item["level"], item["source_key"], item["chain"]))
    return dict(index), closure_sets
