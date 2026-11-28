"""依赖登记加载、校验与传递闭包计算。"""
from __future__ import annotations

import json
from collections import deque


class RegistryError(ValueError):
    pass


class DependencyRegistry:
    def __init__(self, services: set[str], edges: list[tuple[str, str]], display: dict[str, str]):
        self.services = services
        self.edges = edges
        self.display = display
        self._downstream: dict[str, set[str]] = {}
        self._upstream: dict[str, set[str]] = {}
        self._adj: dict[str, set[str]] = {service: set() for service in services}
        self._rev: dict[str, set[str]] = {service: set() for service in services}
        for source, target in edges:
            self._adj[source].add(target)
            self._rev[target].add(source)
        for service in services:
            self._downstream[service] = self._closure(service, self._adj)
            self._upstream[service] = self._closure(service, self._rev)

    @staticmethod
    def _closure(origin: str, graph: dict[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        queue = deque(graph.get(origin, ()))
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(graph.get(node, ()))
        return seen

    def downstream(self, service: str) -> set[str]:
        return set(self._downstream.get(service, set()))

    def upstream(self, service: str) -> set[str]:
        return set(self._upstream.get(service, set()))

    def is_dependency_related(self, service_a: str, service_b: str) -> bool:
        """两个服务是否存在直接或传递依赖关联（任一方向）。"""
        if service_a == service_b:
            return True
        return (
            service_b in self._downstream.get(service_a, set())
            or service_a in self._downstream.get(service_b, set())
        )

    def path_between(self, source: str, target: str) -> list[str]:
        """在依赖图上找一条 source -> ... -> target 的有向路径，用于合并依据。"""
        if source == target:
            return [source]
        queue = deque([(source, [source])])
        seen = {source}
        while queue:
            node, path = queue.popleft()
            for nxt in sorted(self._adj.get(node, set())):
                if nxt in seen:
                    continue
                new_path = path + [nxt]
                if nxt == target:
                    return new_path
                seen.add(nxt)
                queue.append((nxt, new_path))
        return []


def load_registry(path: str) -> DependencyRegistry:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    services = {item["service"] for item in payload.get("services", [])}
    if not services:
        raise RegistryError(f"依赖登记 {path} 中没有任何服务条目")
    display = {item["service"]: item.get("display", item["service"]) for item in payload["services"]}
    edges: list[tuple[str, str]] = []
    for index, edge in enumerate(payload.get("dependencies", [])):
        source, target = edge.get("from"), edge.get("to")
        if source not in services:
            raise RegistryError(f"依赖登记第 {index + 1} 条边的源服务 {source!r} 未在 services 中登记")
        if target not in services:
            raise RegistryError(f"依赖登记第 {index + 1} 条边的目标服务 {target!r} 未在 services 中登记")
        if source == target:
            raise RegistryError(f"依赖登记第 {index + 1} 条边不允许自依赖: {source}")
        edges.append((source, target))
    return DependencyRegistry(services, edges, display)
