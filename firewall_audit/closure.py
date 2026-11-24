"""Incremental dependency closure for snapshot and mapping changes."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable

from .codes import E_MAP_CYCLE
from .mapping import reconcile_mapping
from .models import (
    CaseConclusion,
    MappingResult,
    NormalizedRule,
    SnapshotBundle,
)
from .views import build_views

RuleNode = tuple[str, str, str]
CaseNode = tuple[str, str]
Node = RuleNode | CaseNode


def _rule_node(rule: NormalizedRule) -> RuleNode:
    return ("rule", rule.side, rule.rule_key)


def _case_node(case_id: str) -> CaseNode:
    return ("case", case_id)


def _mapping_for(bundle: SnapshotBundle) -> MappingResult:
    return reconcile_mapping(
        bundle.rules,
        bundle.mapping_entries,
        declaration_generation=bundle.mapping_generation,
        conflict_policy=bundle.conflict_policy,
        direction=bundle.mapping_direction,
    )


def assess_replacement(
    old: SnapshotBundle | None,
    new: SnapshotBundle,
) -> dict[str, Any]:
    if old is None:
        return {
            "replacement": True,
            "reasons": ["initial_snapshot"],
            "old_rule_generation": None,
            "new_rule_generation": new.rule_generation,
            "old_mapping_generation": None,
            "new_mapping_generation": new.mapping_generation,
            "old_rule_set_fingerprint": None,
            "new_rule_set_fingerprint": new.rule_set_fingerprint(),
            "old_mapping_fingerprint": None,
            "new_mapping_fingerprint": new.mapping_fingerprint(),
        }
    reasons: list[str] = []
    if old.rule_generation != new.rule_generation:
        reasons.append("rule_generation_changed")
    if old.mapping_generation != new.mapping_generation:
        reasons.append("mapping_generation_changed")
    if old.rule_set_fingerprint() != new.rule_set_fingerprint():
        reasons.append("canonical_rule_set_changed")
    if old.mapping_fingerprint() != new.mapping_fingerprint():
        reasons.append("mapping_content_changed")
    return {
        "replacement": bool(reasons),
        "reasons": reasons,
        "old_rule_generation": old.rule_generation,
        "new_rule_generation": new.rule_generation,
        "old_mapping_generation": old.mapping_generation,
        "new_mapping_generation": new.mapping_generation,
        "old_rule_set_fingerprint": old.rule_set_fingerprint(),
        "new_rule_set_fingerprint": new.rule_set_fingerprint(),
        "old_mapping_fingerprint": old.mapping_fingerprint(),
        "new_mapping_fingerprint": new.mapping_fingerprint(),
    }


def _changed_rule_nodes(
    old: SnapshotBundle,
    new: SnapshotBundle,
) -> set[RuleNode]:
    def indexed(
        rules: Iterable[NormalizedRule],
    ) -> dict[tuple[str, str, int, int, bool], NormalizedRule]:
        return {
            (
                rule.side,
                rule.rule_key,
                rule.generation,
                rule.effective_ms,
                rule.tombstone,
            ): rule
            for rule in rules
        }

    old_index = indexed(old.rules)
    new_index = indexed(new.rules)
    changed: set[RuleNode] = set()
    for identity in sorted(set(old_index) | set(new_index)):
        before = old_index.get(identity)
        after = new_index.get(identity)
        if before is None or after is None or before.to_dict() != after.to_dict():
            rule = after or before
            assert rule is not None
            changed.add(_rule_node(rule))
    return changed


def _mapping_changes(
    old: MappingResult,
    new: MappingResult,
) -> tuple[set[RuleNode], set[RuleNode]]:
    old_pairs = set(old.pairs)
    new_pairs = set(new.pairs)
    changed_pairs = old_pairs ^ new_pairs
    roots: set[RuleNode] = set()
    # Rule nodes are side-qualified. A changed pair may have a key that exists on
    # both sides, so both side-qualified possibilities are conservative roots.
    for v1_key, v2_key in changed_pairs:
        roots.add(("rule", "v1", v1_key))
        roots.add(("rule", "v2", v2_key))
    cycle_keys = set(old.cycle_rule_keys) | set(new.cycle_rule_keys)
    for key in cycle_keys:
        roots.add(("rule", "v1", key))
        roots.add(("rule", "v2", key))
    return roots, changed_pairs


def _build_dependency_graph(
    old: SnapshotBundle,
    new: SnapshotBundle,
    old_mapping: MappingResult,
    new_mapping: MappingResult,
) -> tuple[dict[Node, set[Node]], set[Node], set[Node]]:
    adjacency: dict[Node, set[Node]] = defaultdict(set)
    rule_nodes: set[Node] = set()
    case_nodes: set[Node] = set()

    all_rules: list[NormalizedRule] = [*old.rules, *new.rules]
    for rule in all_rules:
        node = _rule_node(rule)
        rule_nodes.add(node)
        adjacency[node]

    for mapping in (old_mapping, new_mapping):
        for v1_key, v2_key in mapping.pairs:
            left: Node = ("rule", "v1", v1_key)
            right: Node = ("rule", "v2", v2_key)
            adjacency[left].add(right)
            adjacency[right].add(left)

    cases_by_id: dict[str, list] = defaultdict(list)
    for case in [*old.cases, *new.cases]:
        cases_by_id[case.case_id].append(case)
    for case_id, cases in cases_by_id.items():
        case_node = _case_node(case_id)
        case_nodes.add(case_node)
        adjacency[case_node]
        for case in cases:
            for rule in all_rules:
                if rule.matches(case):
                    adjacency[case_node].add(_rule_node(rule))
                    adjacency[_rule_node(rule)].add(case_node)
    return adjacency, rule_nodes, case_nodes


def _policy_sensitive_cases(bundle: SnapshotBundle) -> set[str]:
    mapping = _mapping_for(bundle)
    views = build_views(bundle.rules)
    sensitive: set[str] = set()
    for case in bundle.cases:
        for view in views:
            hits = [rule for rule in view.active.values() if rule.matches(case)]
            if not hits:
                continue
            minimum_seq = min(rule.rule_seq for rule in hits)
            contenders = [
                rule for rule in hits if rule.rule_seq == minimum_seq
            ]
            if len(contenders) < 2:
                continue
            if len(contenders) == 2:
                first, second = sorted(contenders, key=lambda rule: rule.side)
                if (
                    first.side == "v1"
                    and second.side == "v2"
                    and mapping.are_paired(first.rule_key, second.rule_key)
                    and first.action == second.action
                ):
                    continue
            sensitive.add(case.case_id)
            break
    return sensitive


def _reachable_cases(
    adjacency: dict[Node, set[Node]],
    roots: Iterable[Node],
) -> set[str]:
    seen: set[Node] = set()
    queue: deque[Node] = deque(sorted(roots))
    affected: set[str] = set()
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        if node[0] == "case":
            affected.add(node[1])
        for target in sorted(adjacency.get(node, ())):
            if target not in seen:
                queue.append(target)
    return affected


def compute_closure(
    old: SnapshotBundle | None,
    new: SnapshotBundle,
    old_results: dict[str, CaseConclusion] | None = None,
) -> dict[str, Any]:
    old_results = old_results or {}
    replacement = assess_replacement(old, new)
    new_case_ids = {case.case_id for case in new.cases}
    old_case_ids = {case.case_id for case in old.cases} if old else set()
    removed_cases = sorted(old_case_ids - new_case_ids)

    if old is None:
        affected = sorted(new_case_ids)
        return {
            "codes": [],
            "affected_cases": affected,
            "removed_cases": removed_cases,
            "roots": [f"case:{case_id}" for case_id in affected],
            "reason_counts": {
                "initial_snapshot": len(affected),
                "missing_previous_result": len(affected),
            },
            "mapping_pairs_changed": [],
            "snapshot_replacement": replacement,
            "graph": {"rule_nodes": 0, "case_nodes": len(affected)},
        }

    old_mapping = _mapping_for(old)
    new_mapping = _mapping_for(new)
    changed_rule_nodes = _changed_rule_nodes(old, new)
    mapping_roots, changed_pairs = _mapping_changes(old_mapping, new_mapping)

    old_case_map = old.case_map()
    new_case_map = new.case_map()
    changed_case_nodes: set[CaseNode] = set()
    missing_result_cases: set[str] = set()
    for case_id in sorted(new_case_ids):
        before = old_case_map.get(case_id)
        after = new_case_map[case_id]
        if before is None or before.content_hash() != after.content_hash():
            changed_case_nodes.add(_case_node(case_id))
        if case_id not in old_results:
            missing_result_cases.add(case_id)

    graph, rule_nodes, case_nodes = _build_dependency_graph(
        old, new, old_mapping, new_mapping
    )
    roots: set[Node] = set(changed_rule_nodes) | set(mapping_roots)
    roots |= changed_case_nodes
    if old.conflict_policy != new.conflict_policy:
        sensitive = _policy_sensitive_cases(old) | _policy_sensitive_cases(new)
        roots |= {_case_node(case_id) for case_id in sensitive}
    # If mapping ambiguity/cycle diagnostics change, affected cases must be
    # recomputed for the unified result which can consume mapping resolution.
    if (
        old_mapping.cycle_rule_keys != new_mapping.cycle_rule_keys
        or old_mapping.pairs != new_mapping.pairs
    ):
        roots |= mapping_roots
    affected = _reachable_cases(graph, roots)
    affected |= missing_result_cases
    affected &= new_case_ids

    reason_counts = {
        "changed_rule_records": len(changed_rule_nodes),
        "changed_mapping_pairs": len(changed_pairs),
        "changed_cases": len(changed_case_nodes),
        "missing_previous_result": len(missing_result_cases),
    }
    codes: list[str] = []
    if new_mapping.cycle_rule_keys:
        codes.append(E_MAP_CYCLE)
    root_labels = []
    for node in sorted(roots):
        if node[0] == "rule":
            root_labels.append(f"{node[1]}:{node[2]}")
        else:
            root_labels.append(f"case:{node[1]}")
    return {
        "codes": codes,
        "affected_cases": sorted(affected),
        "removed_cases": removed_cases,
        "roots": root_labels,
        "reason_counts": reason_counts,
        "mapping_pairs_changed": [
            [left, right] for left, right in sorted(changed_pairs)
        ],
        "snapshot_replacement": replacement,
        "graph": {
            "rule_nodes": len(rule_nodes),
            "case_nodes": len(case_nodes),
        },
    }
