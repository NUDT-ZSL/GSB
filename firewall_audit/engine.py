"""Rule matching, case adjudication, and cross-generation equivalence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Iterable

from .codes import (
    E_NOT_EQUIVALENT,
    E_NO_MATCH,
    E_RULE_CONFLICT,
    E_TOMBSTONED,
)
from .mapping import reconcile_mapping
from .models import (
    CaseConclusion,
    CaseTask,
    MappingResult,
    NormalizedRule,
    RuleView,
    SideAdjudication,
    SnapshotBundle,
)
from .views import build_views


def _hit_dict(rule: NormalizedRule) -> dict[str, Any]:
    return {
        "side": rule.side,
        "rule_key": rule.rule_key,
        "action": rule.action,
        "rule_seq": rule.rule_seq,
        "basis_generation": rule.generation,
        "effective_ms": rule.effective_ms,
        "tombstone": rule.tombstone,
        "src_prefix": str(rule.src_network),
        "dst_prefix": str(rule.dst_network),
        "protocol": rule.protocol,
        "src_ports": rule.src_ports.to_list(),
        "dst_ports": rule.dst_ports.to_list(),
    }


def _sorted_hits(rules: Iterable[NormalizedRule]) -> list[NormalizedRule]:
    return sorted(
        rules,
        key=lambda rule: (
            rule.rule_seq,
            rule.generation,
            rule.effective_ms,
            rule.side,
            rule.rule_key,
        ),
    )


def _single_hit(rule: NormalizedRule, *, resolved_by_mapping: bool = False) -> SideAdjudication:
    return SideAdjudication(
        status="hit",
        code=None,
        rule_key=rule.rule_key,
        action=rule.action,
        basis_generation=rule.generation,
        rule_seq=rule.rule_seq,
        hits=(_hit_dict(rule),),
        effective_ms=rule.effective_ms,
        resolved_by_mapping=resolved_by_mapping,
    )


def _anomaly(
    *,
    status: str,
    code: str,
    rules: Iterable[NormalizedRule],
    action: str | None = None,
) -> SideAdjudication:
    ordered = _sorted_hits(rules)
    first = ordered[0] if ordered else None
    return SideAdjudication(
        status=status,
        code=code,
        rule_key=first.rule_key if first else None,
        action=action,
        basis_generation=(
            max(rule.generation for rule in ordered) if ordered else None
        ),
        rule_seq=first.rule_seq if first else None,
        hits=tuple(_hit_dict(rule) for rule in ordered),
        effective_ms=(
            max(rule.effective_ms for rule in ordered) if ordered else None
        ),
    )


def _authority_winner(rules: list[NormalizedRule]) -> NormalizedRule:
    return max(rules, key=lambda rule: rule.authority_key())


def adjudicate_case(
    case: CaseTask,
    view: RuleView,
    mapping: MappingResult,
) -> SideAdjudication:
    active_hits = [
        rule for rule in view.active.values() if rule.matches(case)
    ]
    shadow_hits = [
        rule for rule in view.shadow.values() if rule.matches(case)
    ]
    if not active_hits:
        if shadow_hits:
            return _anomaly(
                status="tombstoned",
                code=E_TOMBSTONED,
                rules=shadow_hits,
            )
        return SideAdjudication(
            status="no_match",
            code=E_NO_MATCH,
            rule_key=None,
            action=None,
            basis_generation=None,
            rule_seq=None,
        )

    minimum_seq = min(rule.rule_seq for rule in active_hits)
    contenders = [rule for rule in active_hits if rule.rule_seq == minimum_seq]
    if len(contenders) == 1:
        return _single_hit(contenders[0])

    # A mapped v1/v2 pair at the same sequence and action is one logical rule,
    # not an unresolved conflict. Keep the authoritative generation.
    if len(contenders) == 2:
        first, second = sorted(contenders, key=lambda rule: rule.side)
        if (
            first.side == "v1"
            and second.side == "v2"
            and mapping.are_paired(first.rule_key, second.rule_key)
            and first.action == second.action
        ):
            return _single_hit(
                _authority_winner(contenders),
                resolved_by_mapping=True,
            )

    policy = mapping.conflict_policy
    narrowed = contenders
    if policy == "deny_wins":
        denies = [rule for rule in contenders if rule.action == "deny"]
        if denies and len(denies) < len(contenders):
            narrowed = denies
    elif policy == "prefer_v2":
        preferred = [rule for rule in contenders if rule.side == "v2"]
        if preferred and len(preferred) < len(contenders):
            narrowed = preferred
    elif policy == "prefer_v1":
        preferred = [rule for rule in contenders if rule.side == "v1"]
        if preferred and len(preferred) < len(contenders):
            narrowed = preferred
    if len(narrowed) == 1:
        return _single_hit(narrowed[0])
    return _anomaly(
        status="conflict",
        code=E_RULE_CONFLICT,
        rules=narrowed,
    )


def _mapped_sets_equivalent(
    v1: SideAdjudication,
    v2: SideAdjudication,
    mapping: MappingResult,
) -> bool:
    if len(v1.hits) != len(v2.hits):
        return False
    v2_by_pair: dict[str, set[str]] = {}
    for hit in v2.hits:
        v2_by_pair.setdefault(hit["rule_key"], set()).add(hit["action"])
    used: set[str] = set()
    for hit in v1.hits:
        candidates = [
            key
            for key in v2_by_pair
            if key not in used and mapping.are_paired(hit["rule_key"], key)
        ]
        if len(candidates) != 1:
            return False
        target = candidates[0]
        if v2_by_pair[target] != {hit["action"]}:
            return False
        used.add(target)
    return len(used) == len(v2_by_pair)


def compare_equivalence(
    case: CaseTask,
    v1: SideAdjudication,
    v2: SideAdjudication,
    v1_view: RuleView,
    v2_view: RuleView,
    mapping: MappingResult,
) -> CaseConclusion:
    unified = adjudicate_case(
        case,
        build_views_for_unified(v1_view, v2_view),
        mapping,
    )
    equivalent = False
    reason = "not_equivalent"
    code: str | None = E_NOT_EQUIVALENT

    if v1.status == "no_match" and v2.status == "no_match":
        equivalent, reason, code = True, "no_match", E_NO_MATCH
    elif v1.status == "hit" and v2.status == "hit":
        if v1.action == v2.action:
            if v1.rule_key == v2.rule_key:
                equivalent, reason, code = True, "same_rule_key", None
            elif v1.rule_key and v2.rule_key and mapping.are_paired(
                v1.rule_key, v2.rule_key
            ):
                equivalent, reason, code = True, "mapping_covered", None
            else:
                left = v1_view.active.get(v1.rule_key or "")
                right = v2_view.active.get(v2.rule_key or "")
                if (
                    left is not None
                    and right is not None
                    and left.semantic_key() == right.semantic_key()
                ):
                    equivalent, reason, code = True, "same_semantics", None
    elif v1.status == "conflict" and v2.status == "conflict":
        left_keys = {(hit["rule_key"], hit["action"]) for hit in v1.hits}
        right_keys = {(hit["rule_key"], hit["action"]) for hit in v2.hits}
        if left_keys == right_keys:
            equivalent, reason, code = True, "same_hit_keys", E_RULE_CONFLICT
        elif _mapped_sets_equivalent(v1, v2, mapping):
            equivalent, reason, code = True, "mapping_covered", E_RULE_CONFLICT
    elif v1.status == "tombstoned" and v2.status == "tombstoned":
        if v1.hit_rule_keys == v2.hit_rule_keys:
            equivalent, reason, code = True, "same_hit_keys", E_TOMBSTONED

    action_diff = None
    if not equivalent:
        action_diff = {
            "v1_action": v1.action,
            "v2_action": v2.action,
            "v1_rule_keys": list(v1.hit_rule_keys),
            "v2_rule_keys": list(v2.hit_rule_keys),
            "different": v1.action != v2.action or v1.hit_rule_keys != v2.hit_rule_keys,
        }
    return CaseConclusion(
        case_id=case.case_id,
        sync_marker=case.sync_marker,
        equivalent=equivalent,
        code=code,
        reason=reason,
        v1=v1,
        v2=v2,
        unified=unified,
        action_diff=action_diff,
        case_hash=case.content_hash(),
    )


def build_views_for_unified(v1_view: RuleView, v2_view: RuleView) -> RuleView:
    grouped: dict[str, list[NormalizedRule]] = {}
    for rule in [*v1_view.active.values(), *v2_view.active.values(),
                 *v1_view.shadow.values(), *v2_view.shadow.values()]:
        grouped.setdefault(rule.rule_key, []).append(rule)
    active: dict[str, NormalizedRule] = {}
    shadow: dict[str, NormalizedRule] = {}
    for key in sorted(grouped):
        winner = _authority_winner(grouped[key])
        if winner.tombstone:
            shadow[key] = winner
        else:
            active[key] = winner
    return RuleView(side="unified", active=active, shadow=shadow)


def evaluate_case(
    case: CaseTask,
    v1_view: RuleView,
    v2_view: RuleView,
    unified_view: RuleView,
    mapping: MappingResult,
) -> CaseConclusion:
    v1_result = adjudicate_case(case, v1_view, mapping)
    v2_result = adjudicate_case(case, v2_view, mapping)
    unified_result = adjudicate_case(case, unified_view, mapping)
    conclusion = compare_equivalence(
        case, v1_result, v2_result, v1_view, v2_view, mapping
    )
    return replace(conclusion, unified=unified_result)


def adjudicate_bundle(
    bundle: SnapshotBundle,
    *,
    case_ids: Iterable[str] | None = None,
    jobs: int = 1,
) -> tuple[dict[str, CaseConclusion], MappingResult]:
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    mapping = reconcile_mapping(
        bundle.rules,
        bundle.mapping_entries,
        declaration_generation=bundle.mapping_generation,
        conflict_policy=bundle.conflict_policy,
        direction=bundle.mapping_direction,
    )
    v1_view, v2_view, unified_view = build_views(bundle.rules)
    case_map = bundle.case_map()
    selected_ids = (
        sorted(case_map)
        if case_ids is None
        else sorted(set(case_ids) & set(case_map))
    )

    def run(case_id: str) -> tuple[str, CaseConclusion]:
        case = case_map[case_id]
        return case_id, evaluate_case(
            case, v1_view, v2_view, unified_view, mapping
        )

    if jobs == 1 or len(selected_ids) <= 1:
        results = [run(case_id) for case_id in selected_ids]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            results = list(executor.map(run, selected_ids))
    results.sort(key=lambda item: item[0])
    return dict(results), mapping


def adjudicate_one(
    bundle: SnapshotBundle,
    case_id: str,
) -> tuple[CaseConclusion, MappingResult]:
    results, mapping = adjudicate_bundle(bundle, case_ids=[case_id])
    return results[case_id], mapping
