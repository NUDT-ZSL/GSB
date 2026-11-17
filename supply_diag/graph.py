"""Deterministic dependency graph construction and cycle discovery."""

from __future__ import annotations

from dataclasses import dataclass

from .manifest import ParsedManifest


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    edge_index: int
    constraint_text: str
    declared_constraint: object

    def stable_key(self) -> tuple:
        return (
            self.source,
            self.target,
            self.constraint_text,
            str(self.declared_constraint),
            self.edge_index,
        )


@dataclass
class DependencyGraph:
    nodes: tuple[str, ...]
    edges: tuple[GraphEdge, ...]
    adjacency: dict[str, tuple[str, ...]]
    reverse_adjacency: dict[str, tuple[str, ...]]
    roots: tuple[str, ...]
    cycles: tuple[tuple[str, ...], ...]
    truncated_cycles: bool = False

    @classmethod
    def from_manifest(cls, manifest: ParsedManifest, *, cycle_limit: int = 10_000) -> "DependencyGraph":
        nodes = tuple(sorted(manifest.components))
        edges: list[GraphEdge] = []
        for component_id in nodes:
            component = manifest.components[component_id]
            for dependency in component.dependencies:
                if dependency.target_id not in manifest.components:
                    continue
                edges.append(
                    GraphEdge(
                        source=component_id,
                        target=dependency.target_id,
                        edge_index=dependency.edge_index,
                        constraint_text=dependency.constraint.canonical_text(),
                        declared_constraint=dependency.declared_constraint,
                    )
                )
        edges.sort(key=lambda edge: edge.stable_key())

        adjacency_lists: dict[str, list[str]] = {node: [] for node in nodes}
        reverse_lists: dict[str, list[str]] = {node: [] for node in nodes}
        indegree: dict[str, int] = {node: 0 for node in nodes}
        for edge in edges:
            adjacency_lists[edge.source].append(edge.target)
            reverse_lists[edge.target].append(edge.source)
            indegree[edge.target] += 1
        adjacency = {
            node: tuple(sorted(set(adjacency_lists[node]))) for node in nodes
        }
        reverse_adjacency = {
            node: tuple(sorted(set(reverse_lists[node]))) for node in nodes
        }
        roots = tuple(node for node in nodes if indegree[node] == 0)
        cycles, truncated = enumerate_cycles(nodes, adjacency, limit=cycle_limit)
        return cls(
            nodes=nodes,
            edges=tuple(edges),
            adjacency=adjacency,
            reverse_adjacency=reverse_adjacency,
            roots=roots,
            cycles=cycles,
            truncated_cycles=truncated,
        )

    def cycle_members(self) -> set[str]:
        members: set[str] = set()
        for cycle in self.cycles:
            members.update(cycle[:-1])
        return members


def enumerate_cycles(
    nodes: tuple[str, ...] | list[str],
    adjacency: dict[str, tuple[str, ...]],
    *,
    limit: int = 10_000,
) -> tuple[tuple[tuple[str, ...], ...], bool]:
    """Enumerate elementary directed cycles in a canonical rotation.

    A cycle starts at its lexicographically smallest node and closes by
    repeating that node at the end.  The result is fully independent of input
    entry order.
    """

    ordered_nodes = tuple(sorted(nodes))
    cycles: set[tuple[str, ...]] = set()
    truncated = False

    for start in ordered_nodes:
        stack: list[tuple[str, tuple[str, ...], frozenset[str]]] = [
            (start, (start,), frozenset({start}))
        ]
        while stack:
            current, path, visited = stack.pop()
            for neighbor in reversed(adjacency.get(current, ())):
                if len(cycles) >= limit:
                    truncated = True
                    break
                if neighbor == start:
                    if len(path) == 1:
                        cycles.add((start, start))
                    elif len(path) > 1:
                        cycles.add(path + (start,))
                    continue
                if neighbor < start or neighbor in visited:
                    continue
                stack.append((neighbor, path + (neighbor,), visited | {neighbor}))
            if truncated:
                break
        if truncated:
            break

    return tuple(sorted(cycles)), truncated


def canonical_path(path: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(path)


def shortest_paths_from_source(
    source: str,
    adjacency: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """Breadth-first deterministic shortest path map."""

    paths: dict[str, tuple[str, ...]] = {source: (source,)}
    frontier: list[str] = [source]
    while frontier:
        next_frontier: list[str] = []
        for node in frontier:
            for neighbor in adjacency.get(node, ()):
                candidate = paths[node] + (neighbor,)
                existing = paths.get(neighbor)
                if existing is None or (len(candidate), candidate) < (len(existing), existing):
                    paths[neighbor] = candidate
                    next_frontier.append(neighbor)
        frontier = sorted(set(next_frontier))
    return paths

