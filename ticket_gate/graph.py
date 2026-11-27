from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass, field
from typing import Any

from .model import Gate, Project, RuleSet


@dataclass(frozen=True)
class DependencyEdge:
    upstream_gate_id: str
    downstream_gate_id: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "upstream_gate_id": self.upstream_gate_id,
            "downstream_gate_id": self.downstream_gate_id,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Conflict:
    gate_id: str
    event_ids: tuple[str, str]
    versions: dict[str, str]
    overlapping_ticket_types: tuple[str, ...]
    fields: tuple[str, ...]
    sources: dict[str, dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "event_ids": list(self.event_ids),
            "versions": dict(self.versions),
            "overlapping_ticket_types": list(self.overlapping_ticket_types),
            "fields": list(self.fields),
            "sources": {
                field_name: dict(paths)
                for field_name, paths in sorted(self.sources.items())
            },
        }


@dataclass(frozen=True)
class BatchPlan:
    batch_no: int
    gate_ids: tuple[str, ...]
    cycle_gate_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_no": self.batch_no,
            "gate_ids": list(self.gate_ids),
            "cycle_gate_ids": list(self.cycle_gate_ids),
        }


@dataclass
class PropagationPlan:
    project_id: str
    affected_gate_ids: tuple[str, ...]
    dependencies: tuple[DependencyEdge, ...]
    batches: tuple[BatchPlan, ...]
    cycles: tuple[tuple[str, ...], ...]
    conflicts: tuple[Conflict, ...]
    graph_hash: str
    gate_prerequisites: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def affected_set(self) -> set[str]:
        return set(self.affected_gate_ids)

    def batch_gate_ids(self) -> list[list[str]]:
        return [list(batch.gate_ids) for batch in self.batches]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "graph_hash": self.graph_hash,
            "affected_gate_ids": list(self.affected_gate_ids),
            "dependencies": [edge.to_dict() for edge in self.dependencies],
            "batches": [batch.to_dict() for batch in self.batches],
            "cycles": [list(cycle) for cycle in self.cycles],
            "multi_subscribed_conflicting_gates": [
                conflict.to_dict() for conflict in self.conflicts
            ],
        }


def affected_gate_ids(project: Project) -> tuple[str, ...]:
    changed_events = set(project.changes)
    result = {
        gate.gate_id
        for gate in project.gates.values()
        if changed_events.intersection(gate.subscriptions)
    }
    return tuple(sorted(result))


def build_gate_dependencies(project: Project) -> tuple[DependencyEdge, ...]:
    gates_by_entrance: dict[str, list[Gate]] = {}
    gates_by_zone: dict[str, list[Gate]] = {}
    for gate in project.gates.values():
        gates_by_entrance.setdefault(gate.entrance_id, []).append(gate)
        for zone_id in gate.seat_zone_ids:
            gates_by_zone.setdefault(zone_id, []).append(gate)

    reasons: dict[tuple[str, str], set[str]] = {}
    for gate in sorted(project.gates.values(), key=lambda item: item.gate_id):
        for upstream_entrance in project.topology.entrance_dependencies.get(
            gate.entrance_id, ()
        ):
            for upstream_gate in gates_by_entrance.get(upstream_entrance, ()):
                if upstream_gate.gate_id == gate.gate_id:
                    continue
                key = (upstream_gate.gate_id, gate.gate_id)
                reasons.setdefault(key, set()).add(f"entrance:{upstream_entrance}")
        for zone_id in gate.seat_zone_ids:
            for upstream_zone in project.topology.seat_zone_dependencies.get(zone_id, ()):
                for upstream_gate in gates_by_zone.get(upstream_zone, ()):
                    if upstream_gate.gate_id == gate.gate_id:
                        continue
                    key = (upstream_gate.gate_id, gate.gate_id)
                    reasons.setdefault(key, set()).add(f"seat_zone:{upstream_zone}")

    return tuple(
        DependencyEdge(upstream, downstream, tuple(sorted(reason_set)))
        for (upstream, downstream), reason_set in sorted(reasons.items())
    )


def _policy_fields(rule: RuleSet) -> dict[str, Any]:
    return {
        "reentry_policy": rule.reentry_policy,
        "child_policy.requires_adult": rule.child_requires_adult,
    }


def detect_conflicts(project: Project, gate_ids: set[str]) -> tuple[Conflict, ...]:
    conflicts: list[Conflict] = []
    for gate_id in sorted(gate_ids):
        gate = project.gates[gate_id]
        subscriptions = list(gate.subscriptions)
        if len(subscriptions) < 2:
            continue
        for left_index in range(len(subscriptions)):
            for right_index in range(left_index + 1, len(subscriptions)):
                left_event = subscriptions[left_index]
                right_event = subscriptions[right_index]
                left_version = project.target_version(left_event)
                right_version = project.target_version(right_event)
                left_rule = project.rule(left_event, left_version)
                right_rule = project.rule(right_event, right_version)
                overlap_start = max(left_rule.start_at, right_rule.start_at)
                overlap_end = min(left_rule.end_at, right_rule.end_at)
                if not overlap_start < overlap_end:
                    continue
                shared_tickets = sorted(
                    set(left_rule.ticket_whitelist).intersection(
                        right_rule.ticket_whitelist
                    )
                )
                if not shared_tickets:
                    continue
                left_policy = _policy_fields(left_rule)
                right_policy = _policy_fields(right_rule)
                differing = tuple(
                    field_name
                    for field_name in left_policy
                    if left_policy[field_name] != right_policy[field_name]
                )
                if not differing:
                    continue
                conflicts.append(
                    Conflict(
                        gate_id=gate_id,
                        event_ids=(left_event, right_event),
                        versions={
                            left_event: left_version,
                            right_event: right_version,
                        },
                        overlapping_ticket_types=tuple(shared_tickets),
                        fields=differing,
                        sources={
                            field_name: {
                                left_event: left_rule.sources[field_name],
                                right_event: right_rule.sources[field_name],
                            }
                            for field_name in differing
                        },
                    )
                )
    return tuple(conflicts)


def _tarjan_scc(
    nodes: tuple[str, ...],
    adjacency: dict[str, set[str]],
) -> list[tuple[str, ...]]:
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

        for neighbor in sorted(adjacency.get(node, ())):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == node:
                    break
            components.append(tuple(sorted(component)))

    for node in nodes:
        if node not in indices:
            strongconnect(node)
    return components


def _build_batches(
    gate_ids: tuple[str, ...],
    edges: tuple[DependencyEdge, ...],
) -> tuple[tuple[BatchPlan, ...], tuple[tuple[str, ...], ...]]:
    node_set = set(gate_ids)
    active_edges = [
        edge
        for edge in edges
        if edge.upstream_gate_id in node_set and edge.downstream_gate_id in node_set
    ]
    adjacency: dict[str, set[str]] = {gate_id: set() for gate_id in gate_ids}
    for edge in active_edges:
        adjacency[edge.upstream_gate_id].add(edge.downstream_gate_id)

    components = _tarjan_scc(gate_ids, adjacency)
    component_by_gate = {
        gate_id: component
        for component in components
        for gate_id in component
    }
    cyclic_components = {
        component
        for component in components
        if len(component) > 1
        or any(
            neighbor == component[0]
            for neighbor in adjacency.get(component[0], ())
        )
    }

    component_edges: dict[tuple[str, ...], set[tuple[str, ...]]] = {
        component: set() for component in components
    }
    component_indegree = {component: 0 for component in components}
    for edge in active_edges:
        upstream_component = component_by_gate[edge.upstream_gate_id]
        downstream_component = component_by_gate[edge.downstream_gate_id]
        if upstream_component == downstream_component:
            continue
        if downstream_component not in component_edges[upstream_component]:
            component_edges[upstream_component].add(downstream_component)
            component_indegree[downstream_component] += 1

    def component_key(component: tuple[str, ...]) -> tuple[str, ...]:
        return component

    ready: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for component, degree in component_indegree.items():
        if degree == 0:
            heapq.heappush(ready, (component_key(component), component))

    batches: list[BatchPlan] = []
    batch_no = 0
    while ready:
        level_components: list[tuple[str, ...]] = []
        while ready:
            level_components.append(heapq.heappop(ready)[1])
        level_components.sort(key=component_key)

        level_gates: list[str] = []
        level_cycle_gates: list[str] = []
        for component in level_components:
            level_gates.extend(component)
            if component in cyclic_components:
                level_cycle_gates.extend(component)

        batches.append(
            BatchPlan(
                batch_no=batch_no,
                gate_ids=tuple(sorted(level_gates)),
                cycle_gate_ids=tuple(sorted(level_cycle_gates)),
            )
        )

        next_level: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        for component in level_components:
            for downstream in sorted(component_edges[component]):
                component_indegree[downstream] -= 1
                if component_indegree[downstream] == 0:
                    next_level.append((component_key(downstream), downstream))
        for key, component in next_level:
            heapq.heappush(ready, (key, component))
        batch_no += 1

    cycles = tuple(
        sorted(component)
        for component in components
        if component in cyclic_components
    )
    return tuple(batches), tuple(sorted(cycles))


def build_plan(project: Project) -> PropagationPlan:
    affected = affected_gate_ids(project)
    all_edges = build_gate_dependencies(project)
    affected_set = set(affected)
    edges = tuple(
        edge
        for edge in all_edges
        if edge.upstream_gate_id in affected_set
        and edge.downstream_gate_id in affected_set
    )
    batches, cycles = _build_batches(affected, edges)
    conflicts = detect_conflicts(project, affected_set)

    prerequisites: dict[str, list[str]] = {gate_id: [] for gate_id in affected}
    for edge in edges:
        prerequisites[edge.downstream_gate_id].append(edge.upstream_gate_id)
    gate_prerequisites = {
        gate_id: tuple(sorted(values)) for gate_id, values in prerequisites.items()
    }

    canonical = {
        "project_id": project.project_id,
        "affected_gate_ids": list(affected),
        "dependencies": [edge.to_dict() for edge in edges],
        "batches": [batch.to_dict() for batch in batches],
        "cycles": [list(cycle) for cycle in cycles],
        "conflicts": [conflict.to_dict() for conflict in conflicts],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return PropagationPlan(
        project_id=project.project_id,
        affected_gate_ids=affected,
        dependencies=edges,
        batches=batches,
        cycles=cycles,
        conflicts=conflicts,
        graph_hash=digest,
        gate_prerequisites=gate_prerequisites,
    )
