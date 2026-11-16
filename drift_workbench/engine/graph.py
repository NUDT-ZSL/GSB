"""Reference dependency graph with deterministic ordering and propagation."""

from __future__ import annotations

from collections import deque
import heapq

from .errors import CycleError


class DependencyGraph:
    """Directed graph ``referenced -> referrer``.

    If resource A references resource B, then an edge ``B -> A`` exists
    because changes to B propagate downstream to A.

    Edges and node sets are kept sorted so every rendered/serialised order is
    reproducible across runs.
    """

    def __init__(self, edges: set[tuple[str, str]] | None = None):
        self._edges: set[tuple[str, str]] = set(edges or set())
        self._nodes: set[str] = set()
        for source, target in self._edges:
            self._nodes.add(source)
            self._nodes.add(target)

    def rebuild(
        self,
        nodes: set[str],
        edges: set[tuple[str, str]],
    ) -> "DependencyGraph":
        graph = DependencyGraph(edges)
        graph._nodes = set(nodes)
        graph._edges = set(edges)
        graph.ensure_acyclic()
        return graph

    @classmethod
    def build(
        cls,
        nodes: set[str],
        edges: set[tuple[str, str]],
    ) -> "DependencyGraph":
        """Construct a validated graph from explicit node and edge sets."""
        graph = cls(edges)
        graph._nodes = set(nodes)
        graph.ensure_acyclic()
        return graph

    @property
    def nodes(self) -> tuple[str, ...]:
        return tuple(sorted(self._nodes))

    @property
    def edges(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._edges))

    def predecessors(self, node: str) -> tuple[str, ...]:
        """Resources referenced directly by ``node``."""
        return tuple(sorted(source for source, target in self._edges if target == node))

    def successors(self, node: str) -> tuple[str, ...]:
        """Direct downstream resources that reference ``node``."""
        return tuple(sorted(target for source, target in self._edges if source == node))

    def referrers(self, node: str) -> tuple[str, ...]:
        return self.successors(node)

    def ensure_acyclic(self) -> None:
        """Raise E_CYCLE with the first deterministically found cycle."""
        cycle = self.find_cycle()
        if cycle:
            raise CycleError(
                f"E_CYCLE: dependency cycle detected: {' -> '.join(cycle)}",
                {"cycle": cycle},
            )

    def find_cycle(self) -> list[str]:
        """Return a cycle path including the repeated node, or [] if acyclic.

        DFS starts from the lexicographically smallest node so the reported
        cycle is stable for identical inputs.
        """
        visiting: set[str] = set()
        visited: set[str] = set()
        stack: list[str] = []

        def dfs(node: str) -> list[str]:
            visiting.add(node)
            stack.append(node)
            for child in self.successors(node):
                if child in visiting:
                    start = stack.index(child)
                    return stack[start:] + [child]
                if child not in visited:
                    found = dfs(child)
                    if found:
                        return found
            stack.pop()
            visiting.remove(node)
            visited.add(node)
            return []

        for node in self.nodes:
            if node not in visited:
                found = dfs(node)
                if found:
                    return found
        return []

    def topological_order(self, subset: set[str] | None = None) -> list[str]:
        """Kahn topological order; ties broken by resource key.

        When ``subset`` is provided, only nodes in the subset are returned,
        while ordering still respects the full graph edges.
        """
        active = set(subset) if subset is not None else set(self._nodes)
        indegree: dict[str, int] = {node: 0 for node in active}
        for source, target in self._edges:
            if source in active and target in active:
                indegree[target] += 1
        ready = [node for node, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            node = heapq.heappop(ready)
            order.append(node)
            for child in self.successors(node):
                if child not in active:
                    continue
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        if len(order) != len(active):
            cycle = self.find_cycle()
            raise CycleError(
                f"E_CYCLE: dependency cycle detected: {' -> '.join(cycle)}",
                {"cycle": cycle},
            )
        return order

    def downstream(self, roots: set[str]) -> tuple[str, ...]:
        """All transitive downstream resources reachable from ``roots``."""
        seen: set[str] = set()
        queue = deque(sorted(roots))
        while queue:
            node = queue.popleft()
            for child in self.successors(node):
                if child not in seen and child not in roots:
                    seen.add(child)
                    queue.append(child)
        return tuple(sorted(seen))

    def propagation_paths(
        self, roots: set[str]
    ) -> dict[str, tuple[str, ...]]:
        """Shortest deterministic propagation path from any root to a node.

        The path includes the root and the downstream node, e.g.
        ``("db", "svc", "edge")``.
        """
        paths: dict[str, tuple[str, ...]] = {
            root: (root,) for root in sorted(roots)
        }
        queue = deque(sorted(roots))
        while queue:
            node = queue.popleft()
            for child in self.successors(node):
                candidate = paths[node] + (child,)
                # BFS first visit is the shortest path; only enqueue then.
                if child not in paths:
                    paths[child] = candidate
                    queue.append(child)
        return {key: paths[key] for key in sorted(paths) if key not in roots}

    def to_dict(self) -> dict[str, object]:
        return {"nodes": list(self.nodes), "edges": [list(edge) for edge in self.edges]}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DependencyGraph":
        nodes = set(data.get("nodes", []))  # type: ignore[arg-type]
        edges = {tuple(edge) for edge in data.get("edges", [])}  # type: ignore[arg-type]
        return cls.build(nodes, edges)
