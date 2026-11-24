"""Dependency graph, cycle detection and reverse invalidation closure."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .codes import E_DEP_CYCLE, E_VERSION_CONSTRAINT
from .model import AuditError
from .versions import satisfies


class DependencyGraph:
    def __init__(self, edges: Iterable[dict[str, Any]] = ()):
        self.edges: list[dict[str, Any]] = []
        self.forward: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.reverse: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.nodes: set[str] = set()
        for edge in edges:
            self.add_edge(edge)

    def add_edge(self, edge: dict[str, Any]) -> None:
        source = edge["from"]
        target = edge["to"]
        self.edges.append(edge)
        self.forward[source].append(edge)
        self.reverse[target].append(edge)
        self.nodes.update({source, target})

    def canonical_edges(self) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    "edge_id": edge.get("edge_id"),
                    "from": edge["from"],
                    "to": edge["to"],
                    "constraint": edge.get("constraint", "*"),
                }
                for edge in self.edges
            ),
            key=lambda e: (e["from"], e["to"], e.get("constraint", ""), str(e.get("edge_id", ""))),
        )

    def find_cycles(self) -> list[list[str]]:
        """Return deterministically rotated cycles, including self loops."""

        adjacency = {node: sorted({edge["to"] for edge in self.forward.get(node, [])}) for node in self.nodes}
        cycles: list[list[str]] = []
        seen_canonical: set[tuple[str, ...]] = set()

        def record_cycle(stack: list[str], start: str) -> None:
            if start not in stack:
                return
            cycle = stack[stack.index(start) :] + [start]
            if len(cycle) == 2 and cycle[0] == cycle[1]:
                oriented = [cycle[0], cycle[0]]
            else:
                core = cycle[:-1]
                min_index = min(range(len(core)), key=lambda i: core[i])
                rotated = core[min_index:] + core[:min_index]
                oriented = rotated + [rotated[0]]
            key = tuple(oriented)
            if key not in seen_canonical:
                seen_canonical.add(key)
                cycles.append(oriented)

        color: dict[str, int] = {}
        stack: list[str] = []

        def dfs(node: str) -> None:
            color[node] = 1
            stack.append(node)
            for nxt in adjacency.get(node, []):
                if nxt not in self.nodes:
                    self.nodes.add(nxt)
                state = color.get(nxt, 0)
                if state == 0:
                    dfs(nxt)
                elif state == 1:
                    record_cycle(stack, nxt)
            stack.pop()
            color[node] = 2

        for node in sorted(self.nodes):
            if color.get(node, 0) == 0:
                dfs(node)
        return sorted(cycles, key=lambda cycle: "→".join(cycle))

    def require_acyclic(self) -> None:
        cycles = self.find_cycles()
        if cycles:
            raise AuditError(E_DEP_CYCLE, f"dependency cycle detected: {cycles[0]}")

    def reverse_closure(self, seeds: Iterable[str]) -> tuple[set[str], dict[str, list[str]]]:
        """Return seed-inclusive closure and deterministic reverse paths."""

        seed_set = set(seeds)
        visited: set[str] = set(seed_set)
        queue = sorted(seed_set)
        parents: dict[str, set[str]] = defaultdict(set)
        while queue:
            current = queue.pop(0)
            for edge in sorted(self.reverse.get(current, []), key=lambda e: (e["from"], e["to"])):
                dependent = edge["from"]
                parents[dependent].add(current)
                if dependent not in visited:
                    visited.add(dependent)
                    queue.append(dependent)
                    queue.sort()

        paths: dict[str, list[str]] = {}

        def build_paths(node: str, trail: tuple[str, ...]) -> list[list[str]]:
            upstream = sorted(parents.get(node, set()))
            if not upstream:
                return [list(trail + (node,))]
            result: list[list[str]] = []
            for parent in upstream:
                if parent in trail:
                    continue
                result.extend(build_paths(parent, trail + (node,)))
            return result

        for node in sorted(visited - seed_set):
            node_paths = build_paths(node, ())
            if node_paths:
                paths[node] = sorted(" -> ".join(path) for path in node_paths)[:8]
        return visited, paths

    def version_violations(self, versions: dict[str, str | None]) -> list[dict[str, Any]]:
        violations: list[dict[str, Any]] = []
        for edge in self.canonical_edges():
            target = edge["to"]
            actual = versions.get(target)
            constraint = edge.get("constraint", "*")
            try:
                satisfied = satisfies(actual, constraint) if actual is not None else False
            except AuditError as exc:
                violations.append(
                    {
                        "edge_id": edge["edge_id"],
                        "from": edge["from"],
                        "to": target,
                        "constraint": constraint,
                        "actual_version": actual,
                        "satisfied": False,
                        "code": exc.code,
                        "message": exc.message,
                    }
                )
                continue
            if not satisfied:
                violations.append(
                    {
                        "edge_id": edge["edge_id"],
                        "from": edge["from"],
                        "to": target,
                        "constraint": constraint,
                        "actual_version": actual,
                        "satisfied": False,
                        "code": E_VERSION_CONSTRAINT,
                    }
                )
        return violations

    def incoming_edges(self, keys: Iterable[str]) -> list[dict[str, Any]]:
        key_set = set(keys)
        return [edge for edge in self.canonical_edges() if edge["to"] in key_set or edge["from"] in key_set]
