"""Dependency-cycle detection and tombstone/dependency invalidation closure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import RuleView


class RuleCycleError(ValueError):
    code = "E_RULE_CYCLE"

    def __init__(self, rules: list[str]):
        self.rules = sorted(set(rules))
        super().__init__("E_RULE_CYCLE: " + ", ".join(self.rules))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "rules": self.rules}


@dataclass(frozen=True)
class ClosureResult:
    invalid_rules: tuple[str, ...]
    direct_invalid: tuple[str, ...]
    reasons: dict[str, list[str]]
    cycle: tuple[str, ...]
    affected_connections: tuple[str, ...] = ()
    invalid_rule_to_connections: dict[str, tuple[str, ...]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invalid_rules": list(self.invalid_rules),
            "direct_invalid": list(self.direct_invalid),
            "reasons": {
                key: list(value)
                for key, value in sorted(self.reasons.items())
            },
            "cycle": {
                "code": "E_RULE_CYCLE",
                "rules": list(self.cycle),
            }
            if self.cycle
            else None,
            "affected_connections": list(self.affected_connections),
            "invalid_rule_to_connections": {
                key: list(value)
                for key, value in sorted(
                    (self.invalid_rule_to_connections or {}).items()
                )
            },
        }


def _tarjan_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in sorted(graph.get(node, set())):
            if neighbor not in graph:
                continue
            if neighbor not in indices:
                visit(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif neighbor in on_stack:
                lowlink[node] = min(lowlink[node], indices[neighbor])
        if lowlink[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            component.sort()
            if len(component) > 1 or (
                len(component) == 1 and component[0] in graph.get(component[0], set())
            ):
                components.append(component)

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(components, key=lambda component: component[0])


def dependency_graph(view: RuleView) -> dict[str, set[str]]:
    return {
        name: set(entity.winner.depends_on)
        for name, entity in sorted(view.entities.items())
    }


def find_cycles(view: RuleView) -> list[list[str]]:
    return _tarjan_cycles(dependency_graph(view))


def compute_invalid_closure(view: RuleView) -> ClosureResult:
    """Compute direct and transitive invalid rules without import-order bias."""

    cycles = find_cycles(view)
    graph = dependency_graph(view)
    reverse: dict[str, set[str]] = {name: set() for name in graph}
    for name, dependencies in graph.items():
        for dependency in dependencies:
            if dependency in reverse:
                reverse[dependency].add(name)

    direct: set[str] = set()
    reasons: dict[str, set[str]] = {}
    cycle_rules = tuple(
        sorted({name for component in cycles for name in component})
    )
    for name in cycle_rules:
        direct.add(name)
        reasons.setdefault(name, set()).add("dependency_cycle")
    for name, entity in sorted(view.entities.items()):
        if entity.winner.tombstone:
            direct.add(name)
            reasons.setdefault(name, set()).add("tombstoned")
        elif not entity.winner.enabled:
            direct.add(name)
            reasons.setdefault(name, set()).add("disabled")
        if any(dependency not in view.entities for dependency in entity.winner.depends_on):
            direct.add(name)
            reasons.setdefault(name, set()).add("unknown_dependency")

    invalid = set(direct)
    queue = sorted(direct)
    cursor = 0
    while cursor < len(queue):
        current = queue[cursor]
        cursor += 1
        for dependent in sorted(reverse.get(current, set())):
            if dependent not in invalid:
                invalid.add(dependent)
                queue.append(dependent)
    for name in sorted(invalid - direct):
        reasons.setdefault(name, set()).add("transitive_dependency_invalid")

    return ClosureResult(
        invalid_rules=tuple(sorted(invalid)),
        direct_invalid=tuple(sorted(direct)),
        reasons={name: sorted(values) for name, values in sorted(reasons.items())},
        cycle=cycle_rules,
    )


def attach_affected_connections(
    closure: ClosureResult,
    connection_hits: dict[str, set[str]],
) -> ClosureResult:
    affected: set[str] = set()
    mapping: dict[str, set[str]] = {}
    invalid = set(closure.invalid_rules)
    for connection_id, hit_rules in sorted(connection_hits.items()):
        overlap = sorted(hit_rules & invalid)
        if overlap:
            affected.add(connection_id)
        for rule in overlap:
            mapping.setdefault(rule, set()).add(connection_id)
    return ClosureResult(
        invalid_rules=closure.invalid_rules,
        direct_invalid=closure.direct_invalid,
        reasons=closure.reasons,
        cycle=closure.cycle,
        affected_connections=tuple(sorted(affected)),
        invalid_rule_to_connections={
            key: tuple(sorted(value)) for key, value in sorted(mapping.items())
        },
    )

