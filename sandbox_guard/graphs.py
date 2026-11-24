"""Deterministic graph cycle detection and stable traversal."""

from __future__ import annotations

from collections import deque
from typing import Hashable, Iterable, Mapping


Node = str


def normalized_graph(edges: Mapping[Node, Iterable[Node]]) -> dict[Node, list[Node]]:
    graph: dict[Node, set[Node]] = {}
    for source, targets in edges.items():
        graph.setdefault(source, set()).update(targets)
        for target in targets:
            graph.setdefault(target, set())
    return {node: sorted(targets) for node, targets in graph.items()}


def find_cycles(edges: Mapping[Node, Iterable[Node]]) -> list[dict[str, object]]:
    """Return stable SCC cycle descriptors.

    A self-loop or an SCC with more than one node is cyclic.  The returned
    keys are sorted and cycle records are sorted by their canonical key list.
    """

    graph = normalized_graph(edges)
    indices: dict[Node, int] = {}
    low: dict[Node, int] = {}
    stack: list[Node] = []
    on_stack: set[Node] = set()
    counter = 0
    sccs: list[list[Node]] = []

    def visit(node: Node) -> None:
        nonlocal counter
        indices[node] = counter
        low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)

        for target in graph[node]:
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], indices[target])

        if low[node] == indices[node]:
            component: list[Node] = []
            while True:
                current = stack.pop()
                on_stack.remove(current)
                component.append(current)
                if current == node:
                    break
            sccs.append(component)

    for node in sorted(graph):
        if node not in indices:
            visit(node)

    cycles: list[dict[str, object]] = []
    for component in sccs:
        keys = sorted(component)
        cyclic = len(keys) > 1
        if len(keys) == 1:
            only = keys[0]
            cyclic = only in graph.get(only, [])
        if not cyclic:
            continue
        edge_list = []
        key_set = set(keys)
        for source in keys:
            for target in graph.get(source, []):
                if target in key_set:
                    edge_list.append({"from": source, "to": target})
        edge_list.sort(key=lambda item: (item["from"], item["to"]))
        cycles.append({"keys": keys, "edges": edge_list})

    cycles.sort(key=lambda cycle: "|".join(cycle["keys"]))
    return cycles


def reverse_edges(edges: Mapping[Node, Iterable[Node]]) -> dict[Node, list[Node]]:
    """Convert dependent -> dependency edges to dependency -> dependents."""

    reverse: dict[Node, set[Node]] = {}
    for source, targets in edges.items():
        reverse.setdefault(source, set())
        for target in targets:
            reverse.setdefault(target, set()).add(source)
    return {node: sorted(targets) for node, targets in reverse.items()}


def propagation_paths(
    reverse_graph: Mapping[Node, Iterable[Node]],
    triggers: Iterable[Node],
) -> tuple[set[Node], dict[Node, list[Node]]]:
    """Return all reached nodes and one stable propagation path per node."""

    graph = normalized_graph(reverse_graph)
    trigger_list = sorted(set(triggers))
    reached: set[Node] = set(trigger_list)
    paths: dict[Node, list[Node]] = {trigger: [trigger] for trigger in trigger_list}

    for trigger in trigger_list:
        queue: deque[Node] = deque([trigger])
        parents: dict[Node, Node | None] = {trigger: None}
        while queue:
            node = queue.popleft()
            for child in graph.get(node, []):
                if child not in parents:
                    parents[child] = node
                    queue.append(child)
        for node, parent in parents.items():
            if node == trigger:
                continue
            path = [node]
            current = parent
            while current is not None:
                path.append(current)
                if current == trigger:
                    break
                current = parents[current]
            path.reverse()
            if node not in paths or tuple(path) < tuple(paths[node]):
                paths[node] = path
            reached.add(node)

    stable_paths = {node: paths[node] for node in sorted(paths)}
    return reached, stable_paths


def forward_closure(
    graph: Mapping[Node, Iterable[Node]],
    roots: Iterable[Node],
) -> set[Node]:
    normalized = normalized_graph(graph)
    seen = set(roots)
    queue = deque(sorted(seen))
    while queue:
        node = queue.popleft()
        for target in normalized.get(node, []):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def stable_sorted_hashable(values: Iterable[Hashable]) -> list[Hashable]:
    return sorted(values, key=lambda value: str(value))
