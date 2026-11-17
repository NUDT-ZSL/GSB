"""Deterministic graph helpers for dependency reachability."""

from __future__ import annotations

from .models import Topology


class TopologyGraph:
    def __init__(self, topology: Topology) -> None:
        self.topology = topology
        adjacency: dict[str, set[str]] = {service: set() for service in topology.services}
        reverse: dict[str, set[str]] = {service: set() for service in topology.services}
        indegree: dict[str, int] = {service: 0 for service in topology.services}
        for edge in topology.edges:
            adjacency.setdefault(edge.upstream, set()).add(edge.downstream)
            reverse.setdefault(edge.downstream, set()).add(edge.upstream)
            indegree[edge.downstream] = indegree.get(edge.downstream, 0) + 1
        self._adjacency = {
            service: tuple(sorted(targets)) for service, targets in sorted(adjacency.items())
        }
        self._reverse = {
            service: tuple(sorted(sources)) for service, sources in sorted(reverse.items())
        }
        self._indegree = dict(sorted(indegree.items()))
        self._reachability: dict[str, frozenset[str]] = {}

    def reachable_from(self, service: str) -> frozenset[str]:
        cached = self._reachability.get(service)
        if cached is not None:
            return cached
        seen: set[str] = set()
        stack = list(reversed(self._adjacency.get(service, ())))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(reversed(self._adjacency.get(current, ())))
        result = frozenset(seen)
        self._reachability[service] = result
        return result

    def upstream_of(self, upstream: str, downstream: str) -> bool:
        return upstream == downstream or downstream in self.reachable_from(upstream)

    def comparable(self, left: str, right: str) -> bool:
        if left == right:
            return True
        return self.upstream_of(left, right) or self.upstream_of(right, left)

    def component(self, service: str) -> frozenset[str]:
        if service not in self.topology.services:
            return frozenset({service})
        seen: set[str] = set()
        stack = [service]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            neighbors = set(self._adjacency.get(current, ()))
            neighbors.update(self._reverse.get(current, ()))
            for neighbor in sorted(neighbors, reverse=True):
                if neighbor not in seen:
                    stack.append(neighbor)
        return frozenset(seen)

    def indegree(self, service: str) -> int:
        return self._indegree.get(service, 0)

    def induced_indegree(self, service: str, services: set[str]) -> int:
        return sum(
            1
            for edge in self.topology.edges
            if edge.downstream == service and edge.upstream in services
        )


def build_adjacency(topology: Topology) -> dict[str, tuple[str, ...]]:
    adjacency: dict[str, list[str]] = {service: [] for service in topology.services}
    for edge in topology.edges:
        adjacency.setdefault(edge.upstream, []).append(edge.downstream)
    return {service: tuple(sorted(targets)) for service, targets in sorted(adjacency.items())}
