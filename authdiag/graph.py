"""Policy dependency graph, cycle detection, and transitive invalidation paths."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Iterable

from .parser import PolicyEntry


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "kind": self.kind}


@dataclass(frozen=True)
class PolicyPath:
    nodes: tuple[str, ...]
    edge_kinds: tuple[str, ...] = ()

    @property
    def root(self) -> str:
        return self.nodes[0] if self.nodes else ""


@dataclass
class Propagation:
    roots: tuple[str, ...]
    affected_entries: tuple[str, ...]
    paths: dict[str, PolicyPath] = field(default_factory=dict)
    cycle_entry_ids: tuple[str, ...] = ()

    def paths_touching_cycle(self) -> bool:
        cycle = set(self.cycle_entry_ids)
        return any(cycle.intersection(path.nodes) for path in self.paths.values())


class DependencyGraph:
    """Edges point from a depender to the policy node it depends on."""

    def __init__(self, entries: Iterable[PolicyEntry]):
        self.entries: dict[str, PolicyEntry] = {
            entry.id: entry for entry in entries
        }
        self.edges: list[GraphEdge] = []
        self.dependencies: dict[str, set[str]] = {
            entry_id: set() for entry_id in self.entries
        }
        self.dependents: dict[str, set[str]] = {
            entry_id: set() for entry_id in self.entries
        }
        for entry in self.sorted_entries:
            for target, kind in sorted(entry.dependency_edges()):
                if target not in self.entries:
                    continue
                edge = GraphEdge(source=entry.id, target=target, kind=kind)
                self.edges.append(edge)
                self.dependencies[entry.id].add(target)
                self.dependents[target].add(entry.id)
        self.edges.sort(key=lambda edge: (edge.source, edge.target, edge.kind))
        self.cycles = self._detect_cycles()
        self.cycle_entry_ids = tuple(
            sorted(
                {entry_id for cycle in self.cycles for entry_id in cycle},
                key=self.entry_sort_key,
            )
        )

    @property
    def sorted_entries(self) -> list[PolicyEntry]:
        return sorted(
            self.entries.values(),
            key=lambda entry: (entry.entry_index, entry.id),
        )

    def entry_sort_key(self, entry_id: str) -> tuple[int, str]:
        entry = self.entries.get(entry_id)
        if entry is None:
            return (2**31 - 1, entry_id)
        return (entry.entry_index, entry.id)

    def propagate(self, changed_entry_ids: Iterable[str]) -> Propagation:
        roots = tuple(
            sorted(set(changed_entry_ids), key=self.entry_sort_key)
        )
        if not roots:
            return Propagation(roots=(), affected_entries=(), cycle_entry_ids=self.cycle_entry_ids)

        best: dict[str, PolicyPath] = {
            root: PolicyPath(nodes=(root,), edge_kinds=()) for root in roots
        }
        heap: list[tuple[int, tuple[str, ...], str]] = [
            (1, (root,), root) for root in roots if root in self.entries
        ]
        heapq.heapify(heap)

        while heap:
            length, nodes, current = heapq.heappop(heap)
            current_path = best.get(current)
            if (
                current_path is None
                or current_path.nodes != nodes
                or len(current_path.nodes) != length
            ):
                continue
            for dependent in sorted(
                self.dependents.get(current, set()),
                key=self.entry_sort_key,
            ):
                candidate_nodes = current_path.nodes + (dependent,)
                for edge in self.edges:
                    if edge.source == dependent and edge.target == current:
                        candidate = PolicyPath(
                            nodes=candidate_nodes,
                            edge_kinds=current_path.edge_kinds + (edge.kind,),
                        )
                        break
                else:
                    continue
                existing = best.get(dependent)
                if existing is None or (
                    len(candidate.nodes),
                    candidate.nodes,
                    candidate.edge_kinds,
                ) < (
                    len(existing.nodes),
                    existing.nodes,
                    existing.edge_kinds,
                ):
                    best[dependent] = candidate
                    heapq.heappush(
                        heap,
                        (len(candidate.nodes), candidate.nodes, dependent),
                    )

        affected = tuple(sorted(best, key=self.entry_sort_key))
        return Propagation(
            roots=roots,
            affected_entries=affected,
            paths=best,
            cycle_entry_ids=self.cycle_entry_ids,
        )

    def best_path_for_dependencies(
        self,
        propagation: Propagation,
        dependencies: Iterable[str],
    ) -> PolicyPath | None:
        candidates: list[PolicyPath] = []
        for dependency in sorted(set(dependencies)):
            path = propagation.paths.get(dependency)
            if path is not None:
                candidates.append(path)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda path: (len(path.nodes), path.nodes, path.edge_kinds),
        )

    def _detect_cycles(self) -> list[tuple[str, ...]]:
        index = 0
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        components: list[tuple[str, ...]] = []

        def strongconnect(node: str) -> None:
            nonlocal index
            indices[node] = index
            lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)

            for target in sorted(
                self.dependencies.get(node, set()),
                key=self.entry_sort_key,
            ):
                if target not in indices:
                    strongconnect(target)
                    lowlinks[node] = min(lowlinks[node], lowlinks[target])
                elif target in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[target])

            if lowlinks[node] == indices[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.remove(member)
                    component.append(member)
                    if member == node:
                        break
                component_sorted = tuple(sorted(component, key=self.entry_sort_key))
                if len(component_sorted) > 1:
                    components.append(component_sorted)
                elif any(
                    edge.source == node and edge.target == node for edge in self.edges
                ):
                    components.append(component_sorted)

        for entry_id in sorted(self.entries, key=self.entry_sort_key):
            if entry_id not in indices:
                strongconnect(entry_id)
        return sorted(
            components,
            key=lambda component: tuple(self.entry_sort_key(item) for item in component),
        )

    def affected_flags(self, affected_entry_ids: Iterable[str]) -> set[str]:
        return set(affected_entry_ids)

    def to_dict(
        self,
        *,
        affected_entry_ids: Iterable[str] = (),
        changed_entry_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        affected = set(affected_entry_ids)
        changed = set(changed_entry_ids)
        nodes = [
            {
                **entry.to_dict(),
                "changed": entry.id in changed,
                "affected": entry.id in affected,
            }
            for entry in self.sorted_entries
        ]
        edges = [
            {
                **edge.to_dict(),
                "source_affected": edge.source in affected,
                "target_affected": edge.target in affected,
            }
            for edge in self.edges
        ]
        cycles = [
            {
                "code": "E_CYCLE",
                "entry_ids": list(cycle),
                "entry_indices": [
                    self.entries[entry_id].entry_index for entry_id in cycle
                ],
                "entry_ordinals": [
                    self.entries[entry_id].entry_index + 1 for entry_id in cycle
                ],
            }
            for cycle in self.cycles
        ]
        return {
            "structure_version": "1.0",
            "nodes": nodes,
            "edges": edges,
            "cycles": cycles,
            "changed_entries": sorted(
                changed & set(self.entries), key=self.entry_sort_key
            ),
            "affected_entries": sorted(
                affected & set(self.entries), key=self.entry_sort_key
            ),
        }

    def to_dot(
        self,
        *,
        affected_entry_ids: Iterable[str] = (),
        changed_entry_ids: Iterable[str] = (),
    ) -> str:
        affected = set(affected_entry_ids)
        changed = set(changed_entry_ids)
        lines = [
            "digraph authdiag {",
            "  rankdir=LR;",
            '  graph [label="Authentication policy dependency graph", labelloc=t];',
            '  node [shape=box, style="rounded,filled", fillcolor="#eef3f8"];',
        ]
        for entry in self.sorted_entries:
            attributes = [
                f'label="{_dot_escape(entry.id)}\\n{entry.kind} v{entry.version}"'
            ]
            if entry.id in changed:
                attributes.extend(['fillcolor="#ffe8a3"', 'penwidth=2'])
            elif entry.id in affected:
                attributes.extend(['fillcolor="#ffd8d8"', 'penwidth=2'])
            lines.append(f'  "{_dot_escape(entry.id)}" [{", ".join(attributes)}];')
        for edge in self.edges:
            color = "#b42318" if edge.source in affected or edge.target in affected else "#667085"
            lines.append(
                f'  "{_dot_escape(edge.source)}" -> "{_dot_escape(edge.target)}" '
                f'[label="{edge.kind}", color="{color}"];'
            )
        for position, cycle in enumerate(self.cycles, start=1):
            lines.append(
                f'  subgraph cluster_cycle_{position} {{ label="E_CYCLE {position}"; '
                f'color="#b42318"; '
                + " ".join(f'"{_dot_escape(item)}";' for item in cycle)
                + " }"
            )
        lines.append("}")
        return "\n".join(lines) + "\n"


def _dot_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
