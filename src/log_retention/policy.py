from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import NormalizedInput, Policy


@dataclass
class PolicyNode:
    policy: Policy
    raw_cutoff_ms: int
    original_time: dict[str, Any]
    conversion: dict[str, Any]
    selected: bool = True
    active: bool = True
    effective: bool = False
    invalidated: bool = False
    merged_from: list[dict[str, Any]] = field(default_factory=list)
    merge_decision: dict[str, Any] = field(default_factory=dict)
    invalidation_reason: str = ""
    parent_key: str | None = None

    @property
    def key(self) -> str:
        return self.policy.key

    @property
    def tombstone(self) -> bool:
        return self.policy.tombstone or self.invalidated

    @property
    def cutoff_ms(self) -> int:
        return self.raw_cutoff_ms

    def covers(self, segment_id: int) -> bool:
        return self.policy.segment_id is None or self.policy.segment_id == segment_id

    def evidence(self) -> dict[str, Any]:
        return {
            "instance_id": self.policy.instance_id,
            "policy_id": self.policy.policy_id,
            "policy_key": self.key,
            "source_version": self.policy.source_version,
            "source_priority": self.policy.source_priority,
            "retention_generation": self.policy.retention_generation,
            "effective_at_ms": self.policy.effective_at_ms,
            "segment_id": self.policy.segment_id,
            "raw_cutoff_ms": self.raw_cutoff_ms,
            "min_catchup_margin_units": self.policy.required_min_catchup_margin,
            "original_time_representation": self.original_time,
            "conversion": self.conversion,
            "parent_policy_key": self.parent_key,
            "tombstone": self.tombstone,
            "selected": self.selected,
            "active": self.active,
            "invalidated": self.invalidated,
            "merged_from": self.merged_from,
            "merge_decision": self.merge_decision,
        }


@dataclass
class Conflict:
    segment_id: int
    instance_id: str
    winner: PolicyNode
    losers: list[PolicyNode]
    candidates: list[PolicyNode]
    common_ancestor_key: str | None
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "instance_id": self.instance_id,
            "winning_policy_key": self.winner.key,
            "winning_policy_id": self.winner.policy.policy_id,
            "losing_policy_keys": [node.key for node in self.losers],
            "losing_policy_ids": [node.policy.policy_id for node in self.losers],
            "common_ancestor_policy_key": self.common_ancestor_key,
            "evidence": self.evidence,
        }


@dataclass
class PolicyView:
    clock_ms: int
    nodes: dict[str, PolicyNode]
    aliases: dict[str, str]
    segment_selected: dict[int, dict[str, PolicyNode]]
    conflicts: list[Conflict]
    edges: list[dict[str, Any]]

    def effective_nodes(self) -> list[PolicyNode]:
        return sorted(
            (node for node in self.nodes.values() if node.effective),
            key=lambda node: (
                node.policy.instance_id,
                -node.policy.retention_generation,
                node.key,
            ),
        )

    def conflicts_for_segment(self, segment_id: int) -> list[Conflict]:
        return [
            conflict for conflict in self.conflicts if conflict.segment_id == segment_id
        ]


def _conversion_for_v1(
    clock_ms: int, relative_seconds: int
) -> tuple[int, dict[str, Any]]:
    # 1 second is exactly 1000 ms, so this conversion has no rounding residue.
    cutoff = clock_ms - relative_seconds * 1000
    return cutoff, {
        "formula": "injected_clock_ms - relative_seconds * 1000",
        "input_relative_seconds": relative_seconds,
        "scale_ms_per_second": 1000,
        "rounding_direction": "exact",
        "rounding_remainder_ms": 0,
        "output_absolute_cutoff_ms": cutoff,
    }


def _conversion_for_v2(policy: Policy) -> tuple[int, dict[str, Any]]:
    assert policy.absolute_cutoff_ms is not None
    return policy.absolute_cutoff_ms, {
        "formula": "absolute_retention_cutoff_ms",
        "rounding_direction": "none",
        "rounding_remainder_ms": 0,
        "output_absolute_cutoff_ms": policy.absolute_cutoff_ms,
    }


def _original_time(policy: Policy) -> dict[str, Any]:
    if policy.source_version == "v1":
        return {
            "kind": "relative_seconds",
            "value": policy.relative_seconds,
            "retention_window": policy.retention_window,
        }
    return {
        "kind": "absolute_ms",
        "value": policy.absolute_cutoff_ms,
        "retention_window": policy.retention_window,
    }


def _resolve_parent_keys(nodes: dict[str, PolicyNode]) -> None:
    by_policy_id: dict[str, list[PolicyNode]] = {}
    for node in nodes.values():
        by_policy_id.setdefault(node.policy.policy_id, []).append(node)
    for node in nodes.values():
        parent_id = node.policy.parent_policy_id
        if parent_id is None:
            node.parent_key = None
            continue
        candidates = by_policy_id.get(parent_id, [])
        same_source = [
            candidate
            for candidate in candidates
            if candidate.policy.source_version == node.policy.source_version
        ]
        if len(same_source) == 1:
            node.parent_key = same_source[0].key
        elif len(candidates) == 1:
            node.parent_key = candidates[0].key
        elif candidates:
            node.parent_key = min(candidates, key=lambda item: item.key).key
        else:
            node.parent_key = None


def _merge_cross_version(
    policies: Iterable[Policy], clock_ms: int
) -> tuple[dict[str, PolicyNode], dict[str, str]]:
    all_nodes: dict[str, PolicyNode] = {}
    by_signature: dict[tuple[str, int | None, int, int], list[PolicyNode]] = {}
    for policy in policies:
        if policy.source_version == "v1":
            assert policy.relative_seconds is not None
            cutoff, conversion = _conversion_for_v1(clock_ms, policy.relative_seconds)
        else:
            cutoff, conversion = _conversion_for_v2(policy)
        node = PolicyNode(
            policy=policy,
            raw_cutoff_ms=cutoff,
            original_time=_original_time(policy),
            conversion=conversion,
        )
        all_nodes[node.key] = node
        by_signature.setdefault(policy.signature, []).append(node)

    aliases: dict[str, str] = {}
    for signature in sorted(by_signature, key=lambda item: repr(item)):
        nodes = by_signature[signature]
        sources = {node.policy.source_version for node in nodes}
        if len(sources) < 2:
            continue
        ranked = sorted(
            nodes,
            key=lambda node: (
                node.policy.source_priority,
                node.policy.source_version,
                node.key,
            ),
            reverse=True,
        )
        winner = ranked[0]
        winner.merge_decision = {
            "reason": "cross_version_latest_source",
            "signature": list(signature),
            "winner": winner.key,
            "ranked_candidates": [
                {
                    "policy_key": node.key,
                    "source_version": node.policy.source_version,
                    "source_priority": node.policy.source_priority,
                    "retention_generation": node.policy.retention_generation,
                    "effective_at_ms": node.policy.effective_at_ms,
                }
                for node in ranked
            ],
        }
        winner.merged_from = [
            {
                "policy_key": node.key,
                "source_version": node.policy.source_version,
                "raw_cutoff_ms": node.raw_cutoff_ms,
                "original_time_representation": node.original_time,
            }
            for node in sorted(nodes, key=lambda n: n.key)
        ]
        for loser in ranked[1:]:
            loser.selected = False
            loser.active = False
            loser.merge_decision = {
                "reason": "cross_version_superseded",
                "winner": winner.key,
                "signature": list(signature),
            }
            aliases[loser.key] = winner.key

    root_groups: dict[tuple[str, int | None], list[PolicyNode]] = {}
    for node in all_nodes.values():
        if node.selected and node.policy.parent_policy_id is None:
            root_groups.setdefault(
                (node.policy.instance_id, node.policy.segment_id), []
            ).append(node)
    for group_key in sorted(root_groups, key=lambda item: repr(item)):
        nodes = root_groups[group_key]
        if len({node.policy.source_version for node in nodes}) < 2:
            continue
        ranked = sorted(
            nodes,
            key=lambda node: (
                node.policy.effective_at_ms,
                node.policy.source_priority,
                node.policy.retention_generation,
                node.key,
            ),
            reverse=True,
        )
        winner = ranked[0]
        merged_candidates = [
            {
                "policy_key": node.key,
                "source_version": node.policy.source_version,
                "source_priority": node.policy.source_priority,
                "retention_generation": node.policy.retention_generation,
                "effective_at_ms": node.policy.effective_at_ms,
                "raw_cutoff_ms": node.raw_cutoff_ms,
                "original_time_representation": node.original_time,
            }
            for node in sorted(nodes, key=lambda n: n.key)
        ]
        winner.merge_decision = {
            "reason": "cross_version_latest_clock_generation",
            "scope": {
                "instance_id": group_key[0],
                "segment_id": group_key[1],
            },
            "winner": winner.key,
            "ranked_candidates": merged_candidates,
            "ranking": "effective_at_ms,source_priority,retention_generation,policy_key",
        }
        known_keys = {item["policy_key"] for item in winner.merged_from}
        for item in merged_candidates:
            if item["policy_key"] not in known_keys:
                winner.merged_from.append(item)
                known_keys.add(item["policy_key"])
        winner.merged_from.sort(key=lambda item: item["policy_key"])
        for loser in ranked[1:]:
            loser.selected = False
            loser.active = False
            loser.merge_decision = {
                "reason": "cross_version_root_superseded",
                "winner": winner.key,
                "scope": {
                    "instance_id": group_key[0],
                    "segment_id": group_key[1],
                },
            }
            aliases[loser.key] = winner.key

    for node in all_nodes.values():
        if node.selected:
            node.active = True
    return all_nodes, aliases


def _resolve_aliases(aliases: dict[str, str], key: str) -> str:
    seen: set[str] = set()
    while key in aliases:
        if key in seen:
            break
        seen.add(key)
        key = aliases[key]
    return key


def _build_tree(
    nodes: dict[str, PolicyNode], aliases: dict[str, str]
) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {
        key: [] for key, node in nodes.items() if node.selected
    }
    for key, node in nodes.items():
        if not node.selected or node.parent_key is None:
            continue
        parent_key = _resolve_aliases(aliases, node.parent_key)
        if parent_key in children:
            children[parent_key].append(key)
    for values in children.values():
        values.sort()

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError(f"policy cycle after cross-version merge at {key}")
        if key in visited:
            return
        visiting.add(key)
        for child_key in children[key]:
            visit(child_key)
        visiting.remove(key)
        visited.add(key)

    for key in sorted(children):
        visit(key)
    return children


def _ancestor_chain(
    node: PolicyNode, nodes: dict[str, PolicyNode], aliases: dict[str, str]
) -> list[str]:
    chain: list[str] = []
    current: str | None = node.key
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        parent_key = nodes[current].parent_key
        if parent_key is None:
            break
        current = _resolve_aliases(aliases, parent_key)
        chain.append(current)
    return chain


def _common_ancestor(
    candidates: list[PolicyNode],
    nodes: dict[str, PolicyNode],
    aliases: dict[str, str],
) -> str | None:
    if not candidates:
        return None
    chains = [[node.key, *_ancestor_chain(node, nodes, aliases)] for node in candidates]
    common: set[str] = set(chains[0])
    for chain in chains[1:]:
        common.intersection_update(chain)
    if not common:
        return None
    return min(common, key=lambda key: (chains[0].index(key), key))


def _descendant_coverings(
    key: str,
    segment_id: int,
    nodes: dict[str, PolicyNode],
    children: dict[str, list[str]],
) -> list[str]:
    result: list[str] = []
    stack = list(children.get(key, []))
    while stack:
        child = stack.pop()
        node = nodes[child]
        if node.covers(segment_id):
            result.append(child)
        stack.extend(children.get(child, []))
    return result


def _rank_candidate(node: PolicyNode) -> tuple[int, int, int, str]:
    return (
        node.policy.retention_generation,
        node.policy.effective_at_ms,
        node.policy.source_priority,
        node.key,
    )


def build_policy_view(
    normalized: NormalizedInput,
    *,
    clock_ms: int,
    invalidations: set[str] | None = None,
) -> PolicyView:
    invalidations = invalidations or set()
    nodes, aliases = _merge_cross_version(normalized.policies, clock_ms)
    _resolve_parent_keys(nodes)
    children = _build_tree(nodes, aliases)

    blocked: set[str] = set()
    for key in sorted(nodes):
        node = nodes[key]
        if not node.selected:
            continue
        if key in invalidations:
            node.invalidated = True
            node.active = False
            node.invalidation_reason = "retention_conflict_invalidated"
            blocked.add(key)
            continue
        parent_key = node.parent_key
        if parent_key is not None:
            parent_key = _resolve_aliases(aliases, parent_key)
            if parent_key in blocked or (
                nodes[parent_key].invalidated if parent_key in nodes else False
            ):
                node.active = False
                node.invalidation_reason = "invalidated_ancestor_branch"
                blocked.add(key)
    # A second pass closes branches even when iteration saw a child first.
    changed = True
    while changed:
        changed = False
        for key, node in nodes.items():
            if not node.selected or node.invalidated or not node.active:
                continue
            if node.parent_key is None:
                continue
            parent_key = _resolve_aliases(aliases, node.parent_key)
            parent = nodes.get(parent_key)
            if parent is not None and parent.invalidated:
                node.active = False
                node.invalidation_reason = "invalidated_ancestor_branch"
                blocked.add(key)
                changed = True

    segment_selected: dict[int, dict[str, PolicyNode]] = {}
    conflicts: list[Conflict] = []
    for segment_id in normalized.segment_ids:
        by_instance: dict[str, list[PolicyNode]] = {}
        for key, node in nodes.items():
            if not node.selected or not node.active:
                continue
            if not node.covers(segment_id):
                continue
            if _descendant_coverings(key, segment_id, nodes, children):
                continue
            by_instance.setdefault(node.policy.instance_id, []).append(node)

        selected_for_segment: dict[str, PolicyNode] = {}
        for instance_id in sorted(by_instance):
            candidates = sorted(
                by_instance[instance_id], key=_rank_candidate, reverse=True
            )
            winner = candidates[0]
            losers = candidates[1:]
            distinct_cutoffs = {candidate.raw_cutoff_ms for candidate in candidates}
            distinct_tombstones = {candidate.tombstone for candidate in candidates}
            if len(candidates) > 1 and (
                len(distinct_cutoffs) > 1 or len(distinct_tombstones) > 1
            ):
                ancestor = _common_ancestor(candidates, nodes, aliases)
                evidence = {
                    "reason": "same_segment_instance_contradictory_policies",
                    "segment_id": segment_id,
                    "instance_id": instance_id,
                    "ranking": "retention_generation,effective_at_ms,source_priority,policy_key",
                    "winner": winner.evidence(),
                    "candidates": [candidate.evidence() for candidate in candidates],
                    "common_ancestor_policy_key": ancestor,
                }
                conflicts.append(
                    Conflict(
                        segment_id=segment_id,
                        instance_id=instance_id,
                        winner=winner,
                        losers=losers,
                        candidates=candidates,
                        common_ancestor_key=ancestor,
                        evidence=evidence,
                    )
                )
            if not winner.tombstone:
                selected_for_segment[instance_id] = winner
        segment_selected[segment_id] = selected_for_segment

    for per_instance in segment_selected.values():
        for node in per_instance.values():
            node.effective = True

    edges: list[dict[str, Any]] = []
    edge_order = 1
    for segment_id in sorted(segment_selected):
        per_instance = segment_selected[segment_id]
        for instance_id in sorted(per_instance):
            node = per_instance[instance_id]
            edges.append(
                {
                    "edge_order": edge_order,
                    "parent_type": "segment",
                    "parent_id": str(segment_id),
                    "child_type": "instance",
                    "child_id": instance_id,
                    "attributes": {
                        "segment_id": segment_id,
                        "instance_id": instance_id,
                        "policy_key": node.key,
                        "raw_cutoff_ms": node.raw_cutoff_ms,
                    },
                }
            )
            edge_order += 1
            edges.append(
                {
                    "edge_order": edge_order,
                    "parent_type": "instance",
                    "parent_id": instance_id,
                    "child_type": "policy",
                    "child_id": node.key,
                    "attributes": {
                        "policy_id": node.policy.policy_id,
                        "source_version": node.policy.source_version,
                        "retention_generation": node.policy.retention_generation,
                        "effective_at_ms": node.policy.effective_at_ms,
                        "raw_cutoff_ms": node.raw_cutoff_ms,
                        "original_time_representation": node.original_time,
                    },
                }
            )
            edge_order += 1

    return PolicyView(
        clock_ms=clock_ms,
        nodes=nodes,
        aliases=aliases,
        segment_selected=segment_selected,
        conflicts=conflicts,
        edges=edges,
    )


def descendant_keys(view: PolicyView, policy_key: str) -> list[str]:
    children: dict[str, list[str]] = {key: [] for key in view.nodes}
    for key, node in view.nodes.items():
        if node.parent_key is None:
            continue
        parent = _resolve_aliases(view.aliases, node.parent_key)
        if parent in children:
            children[parent].append(key)
    result: list[str] = []
    stack = list(children.get(policy_key, []))
    while stack:
        current = stack.pop()
        result.append(current)
        stack.extend(children.get(current, []))
    return sorted(result)
