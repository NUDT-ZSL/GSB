"""Object inheritance chains and trustee group graph expansion."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .models import GroupEdge, ObjectDecl


@dataclass
class ObjectChain:
    objects: list[ObjectDecl]
    cycle: list[str]


def object_inheritance_chain(
    object_key: str, objects_by_key: dict[str, ObjectDecl]
) -> ObjectChain:
    chain: list[ObjectDecl] = []
    visited: list[str] = []
    seen: set[str] = set()
    current_key: str | None = object_key

    while current_key is not None:
        if current_key in seen:
            start = visited.index(current_key)
            return ObjectChain(chain, visited[start:] + [current_key])
        obj = objects_by_key.get(current_key)
        if obj is None:
            break
        seen.add(current_key)
        visited.append(current_key)
        chain.append(obj)
        if obj.inheritance_mode != "ALLOW" or obj.parent_key is None:
            break
        current_key = obj.parent_key

    return ObjectChain(chain, [])


class GroupGraph:
    def __init__(self, edges: Iterable[GroupEdge | tuple[str, str]] = ()) -> None:
        self.children: dict[str, set[str]] = defaultdict(set)
        self.parents: dict[str, set[str]] = defaultdict(set)
        self.nodes: set[str] = set()
        for edge in edges:
            if isinstance(edge, tuple):
                parent, child = edge
            else:
                parent, child = edge.parent_id, edge.child_id
            self.add_edge(parent, child)

    def add_edge(self, parent: str, child: str) -> None:
        self.children[parent].add(child)
        self.parents[child].add(parent)
        self.nodes.add(parent)
        self.nodes.add(child)

    def remove_edge(self, parent: str, child: str) -> None:
        self.children[parent].discard(child)
        self.parents[child].discard(parent)

    def sorted_children(self, node: str) -> list[str]:
        return sorted(self.children.get(node, set()))

    def detect_cycles(self) -> list[list[str]]:
        """Return deterministic SCC cycle member lists, including self loops."""
        index = 0
        indices: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        sccs: list[list[str]] = []

        def strongconnect(node: str) -> None:
            nonlocal index
            indices[node] = index
            low[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)

            for child in self.sorted_children(node):
                if child not in indices:
                    strongconnect(child)
                    low[node] = min(low[node], low[child])
                elif child in on_stack:
                    low[node] = min(low[node], indices[child])

            if low[node] == indices[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.remove(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    sccs.append(sorted(component))
                elif node in self.children.get(node, set()):
                    sccs.append([node])

        for node in sorted(self.nodes):
            if node not in indices:
                strongconnect(node)

        sccs.sort(key=lambda members: (members[0], len(members), ",".join(members)))
        return sccs

    @property
    def cyclic_nodes(self) -> set[str]:
        result: set[str] = set()
        for component in self.detect_cycles():
            result.update(component)
        return result

    def descendants(self, start: str, *, exclude_cyclic: set[str] | None = None) -> set[str]:
        excluded = exclude_cyclic or set()
        if start in excluded:
            return set()
        found: set[str] = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for child in self.sorted_children(node):
                if child in excluded or child in found:
                    continue
                found.add(child)
                stack.append(child)
        return found

    def ancestors(self, node: str) -> set[str]:
        found: set[str] = {node}
        stack = [node]
        while stack:
            current = stack.pop()
            for parent in sorted(self.parents.get(current, set())):
                if parent not in found:
                    found.add(parent)
                    stack.append(parent)
        return found

    def canonical_path(
        self, start: str, target: str, *, exclude_cyclic: set[str] | None = None
    ) -> list[str] | None:
        excluded = exclude_cyclic or set()
        if start in excluded or target in excluded:
            # A clean path cannot start from or pass through a cyclic SCC.
            return None
        if start == target:
            return [start]
        queue: list[list[str]] = [[start]]
        seen: set[str] = {start}
        while queue:
            path = queue.pop(0)
            node = path[-1]
            for child in self.sorted_children(node):
                if child in excluded:
                    continue
                if child in seen:
                    continue
                next_path = path + [child]
                if child == target:
                    return next_path
                seen.add(child)
                queue.append(next_path)
        return None

    def expansion_for_grant(
        self, trustee_id: str, cycles: set[str] | None = None
    ) -> dict[str, dict[str, object]]:
        """Map reachable trustee -> canonical path and cycle-taint flag."""
        cyclic = set(cycles if cycles is not None else self.cyclic_nodes)
        full = self.descendants(trustee_id)
        clean = self.descendants(trustee_id, exclude_cyclic=cyclic)
        is_group = trustee_id.startswith("group:")
        result: dict[str, dict[str, object]] = {}
        for target in sorted(full):
            # A direct grant to a group is expanded to members.  The group
            # itself is retained only when a path reaches it as a nested
            # member or when it is inside a cyclic SCC and must be flagged.
            if is_group and target == trustee_id and trustee_id not in cyclic:
                continue
            clean_path = None if target in cyclic else self.canonical_path(
                trustee_id, target, exclude_cyclic=cyclic
            )
            if clean_path is not None and target in clean:
                result[target] = {"path": clean_path, "tainted": False}
                continue
            tainted_path = self.canonical_path(trustee_id, target)
            result[target] = {
                "path": tainted_path or [trustee_id, target],
                "tainted": True,
            }
        return result
