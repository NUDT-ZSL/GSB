"""Deterministic dependency graph construction."""

from __future__ import annotations

from .errors import E_DEPENDENCY_CYCLE, BusinessError


def build_graph(
    names: list[str],
    upstream: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    downstream: dict[str, list[str]] = {name: [] for name in names}
    indegree = {name: len(upstream[name]) for name in names}
    for name in names:
        for dependency in upstream[name]:
            downstream[dependency].append(name)
    for name in names:
        downstream[name].sort()

    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for child in downstream[name]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(order) != len(names):
        cyclic = sorted(name for name, degree in indegree.items() if degree > 0)
        raise BusinessError(
            E_DEPENDENCY_CYCLE,
            "服务依赖图存在环",
            {"cycle_candidates": cyclic},
        )
    return order, upstream, downstream


def transitive_downstream(
    order: list[str],
    downstream: dict[str, list[str]],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name in reversed(order):
        closure: set[str] = set()
        for child in downstream[name]:
            closure.add(child)
            closure.update(result[child])
        result[name] = sorted(closure)
    return result


def paths_to(
    source: str,
    target: str,
    downstream: dict[str, list[str]],
) -> list[list[str]]:
    if source == target:
        return [[source]]
    paths: list[list[str]] = []
    stack: list[tuple[str, list[str]]] = [(source, [source])]
    while stack:
        node, path = stack.pop()
        for child in reversed(downstream[node]):
            next_path = path + [child]
            if child == target:
                paths.append(next_path)
            elif len(next_path) <= len(downstream) + 1:
                stack.append((child, next_path))
    return sorted(paths)
