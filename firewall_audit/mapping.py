"""Order-independent bidirectional mapping reconciliation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .codes import E_MAP_AMBIGUOUS, E_MAP_CYCLE, E_MAP_EXTRA, E_MAP_MISSING
from .models import (
    MappingEntry,
    MappingIssue,
    MappingResult,
    NormalizedRule,
)
from .views import build_side_view


def _resolve_generations(entries: Iterable[MappingEntry]) -> list[MappingEntry]:
    """Keep only declarations at the highest generation for every involved key.

    The fixed-point filter is independent of declaration arrival order. Inactive
    entries participate in generation selection so that a newer withdrawal can
    supersede an older active mapping without requiring an explicit deletion API.
    """

    candidates = sorted(
        entries,
        key=lambda entry: (
            entry.v1_rule_key,
            entry.v2_rule_key,
            entry.mapping_generation,
            entry.record_index,
            entry.active,
        ),
    )
    while True:
        max_v1: dict[str, int] = defaultdict(lambda: -1)
        max_v2: dict[str, int] = defaultdict(lambda: -1)
        for entry in candidates:
            max_v1[entry.v1_rule_key] = max(
                max_v1[entry.v1_rule_key], entry.mapping_generation
            )
            max_v2[entry.v2_rule_key] = max(
                max_v2[entry.v2_rule_key], entry.mapping_generation
            )
        kept = [
            entry
            for entry in candidates
            if entry.mapping_generation == max_v1[entry.v1_rule_key]
            and entry.mapping_generation == max_v2[entry.v2_rule_key]
        ]
        if len(kept) == len(candidates):
            return kept
        candidates = kept


def _strongly_connected_components(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> list[list[str]]:
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)
        adjacency.setdefault(target, [])
    for values in adjacency.values():
        values.sort()
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in adjacency[node]:
            if target not in indices:
                visit(target)
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
            components.append(sorted(component))

    for node in sorted(adjacency):
        if node not in indices:
            visit(node)
    return components


def reconcile_mapping(
    rules: Iterable[NormalizedRule],
    entries: Iterable[MappingEntry],
    *,
    declaration_generation: int,
    conflict_policy: str,
    direction: str,
) -> MappingResult:
    rule_tuple = tuple(rules)
    v1_view = build_side_view("v1", rule_tuple)
    v2_view = build_side_view("v2", rule_tuple)
    all_v1 = {rule.rule_key for rule in rule_tuple if rule.side == "v1"}
    all_v2 = {rule.rule_key for rule in rule_tuple if rule.side == "v2"}

    selected_all = _resolve_generations(entries)
    selected = [entry for entry in selected_all if entry.active]
    selected.sort(
        key=lambda entry: (
            entry.v1_rule_key,
            entry.v2_rule_key,
            entry.mapping_generation,
            entry.record_index,
        )
    )
    # Duplicate declarations at the same generation are one mapping edge. If an
    # active and an inactive declaration tie, the inactive withdrawal wins in a
    # deterministic way.
    pair_active: dict[tuple[str, str], bool] = {}
    for entry in selected_all:
        pair = (entry.v1_rule_key, entry.v2_rule_key)
        pair_active[pair] = pair_active.get(pair, True) and entry.active
    pairs = tuple(sorted(pair for pair, is_active in pair_active.items() if is_active))
    pair_set = frozenset(pairs)
    v1_to_v2_lists: dict[str, list[str]] = defaultdict(list)
    v2_to_v1_lists: dict[str, list[str]] = defaultdict(list)
    for v1_key, v2_key in pairs:
        v1_to_v2_lists[v1_key].append(v2_key)
        v2_to_v1_lists[v2_key].append(v1_key)
    v1_to_v2 = {
        key: tuple(sorted(values)) for key, values in sorted(v1_to_v2_lists.items())
    }
    v2_to_v1 = {
        key: tuple(sorted(values)) for key, values in sorted(v2_to_v1_lists.items())
    }

    unknown_v1 = sorted(
        {v1_key for v1_key, _ in pairs if v1_key not in all_v1}
        | {entry.v1_rule_key for entry in selected if entry.v1_rule_key not in all_v1}
    )
    unknown_v2 = sorted(
        {v2_key for _, v2_key in pairs if v2_key not in all_v2}
        | {entry.v2_rule_key for entry in selected if entry.v2_rule_key not in all_v2}
    )

    issues: list[MappingIssue] = []
    missing = sorted(
        set(unknown_v1)
        | {
            key
            for key in v1_view.active
            if not any(target in all_v2 for target in v1_to_v2.get(key, ()))
        }
    )
    if missing:
        issues.append(
            MappingIssue(
                code=E_MAP_MISSING,
                rule_keys=tuple(missing),
                message="active v1 rules have no existing, uniquely mapped v2 rule",
            )
        )
    extra = sorted(
        set(unknown_v2)
        | {
            key
            for key in v2_view.active
            if not any(source in v1_view.active for source in v2_to_v1.get(key, ()))
        }
    )
    if extra:
        issues.append(
            MappingIssue(
                code=E_MAP_EXTRA,
                rule_keys=tuple(extra),
                message="active v2 rules have no active, uniquely mapped v1 rule",
            )
        )

    ambiguous_v1 = {
        key: values for key, values in v1_to_v2.items() if len(values) > 1
    }
    ambiguous_v2 = {
        key: values for key, values in v2_to_v1.items() if len(values) > 1
    }
    for key, values in sorted(ambiguous_v1.items()):
        issues.append(
            MappingIssue(
                code=E_MAP_AMBIGUOUS,
                rule_keys=tuple(sorted({key, *values})),
                message=f"v1 rule {key!r} is mapped to multiple v2 rules",
                pairs=tuple(sorted((key, target) for target in values)),
            )
        )
    for key, values in sorted(ambiguous_v2.items()):
        issues.append(
            MappingIssue(
                code=E_MAP_AMBIGUOUS,
                rule_keys=tuple(sorted({*values, key})),
                message=f"v2 rule {key!r} is mapped from multiple v1 rules",
                pairs=tuple(sorted((source, key) for source in values)),
            )
        )

    graph_edges = [
        (v1_key, v2_key)
        for v1_key, v2_key in pairs
        if v1_key != v2_key
    ]
    graph_nodes = set(all_v1) | set(all_v2)
    for source, target in graph_edges:
        graph_nodes.add(source)
        graph_nodes.add(target)
    cycle_components = [
        component
        for component in _strongly_connected_components(graph_nodes, graph_edges)
        if len(component) > 1
    ]
    cycle_rule_keys = tuple(
        sorted({key for component in cycle_components for key in component})
    )
    if cycle_rule_keys:
        issues.append(
            MappingIssue(
                code=E_MAP_CYCLE,
                rule_keys=cycle_rule_keys,
                message="mapping dependency graph contains a directed cycle",
            )
        )

    code_order = {
        E_MAP_MISSING: 0,
        E_MAP_EXTRA: 1,
        E_MAP_AMBIGUOUS: 2,
        E_MAP_CYCLE: 3,
    }
    issues.sort(key=lambda issue: (code_order[issue.code], issue.rule_keys))
    return MappingResult(
        declaration_generation=declaration_generation,
        conflict_policy=conflict_policy,
        direction=direction,
        entries=tuple(sorted(entries, key=lambda item: item.record_index)),
        selected_entries=tuple(selected),
        pairs=pairs,
        pair_set=pair_set,
        v1_to_v2=v1_to_v2,
        v2_to_v1=v2_to_v1,
        issues=tuple(issues),
        cycle_rule_keys=cycle_rule_keys,
        unknown_v1_keys=tuple(unknown_v1),
        unknown_v2_keys=tuple(unknown_v2),
    )
