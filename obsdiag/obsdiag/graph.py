"""拓扑图：邻接表、入度与带路径的可达性。

边语义：``source -> target`` 表示 source 依赖 target；
故障沿 target -> source 方向向上游传播。
所有遍历均按服务名/权重排序后进行，路径选择确定，不依赖哈希顺序。
"""
from __future__ import annotations

from collections import deque

from .models import PathLink, Topology


class TopologyGraph:
    def __init__(self, topology: Topology):
        self.topology = topology
        self.adjacent: dict[str, list[tuple[str, int]]] = {service: [] for service in topology.services}
        self.reverse: dict[str, list[tuple[str, int]]] = {service: [] for service in topology.services}
        for source, target, weight in topology.edges:
            self.adjacent[source].append((target, weight))
            self.reverse[target].append((source, weight))
        for mapping in (self.adjacent, self.reverse):
            for service in mapping:
                mapping[service].sort()
        self.global_in_degree = {
            service: len(self.reverse[service]) for service in topology.services
        }
        self._reach_cache: dict[tuple[str, str], tuple[PathLink, ...] | None] = {}

    def shortest_path(self, source: str, target: str) -> tuple[PathLink, ...] | None:
        """返回 source -> target 的确定性最短依赖路径；不可达返回 None。"""
        if source == target:
            return ()
        cached = self._reach_cache.get((source, target))
        if cached is not None or (source, target) in self._reach_cache:
            return cached
        predecessors: dict[str, tuple[str, int]] = {}
        visited = {source}
        queue: deque[str] = deque([source])
        while queue:
            current = queue.popleft()
            if current == target:
                break
            for neighbor, weight in self.adjacent[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    predecessors[neighbor] = (current, weight)
                    queue.append(neighbor)
        if target not in predecessors:
            self._reach_cache[(source, target)] = None
            return None
        links: list[PathLink] = []
        cursor = target
        while cursor != source:
            previous, weight = predecessors[cursor]
            links.append(PathLink(source=previous, target=cursor, weight=weight))
            cursor = previous
        links.reverse()
        path = tuple(links)
        self._reach_cache[(source, target)] = path
        return path

    def local_in_degree(self, service: str, member_services: frozenset[str]) -> int:
        """簇内入度：指向该服务、且起点也在簇内的依赖边数量。"""
        return sum(1 for source, _ in self.reverse[service] if source in member_services)
