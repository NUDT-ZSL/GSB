from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .masking import canonical_field_path
from .models import (
    CategoryDefault,
    LayoutPolicy,
    RuleConflict,
    RuleRecord,
    UnifiedRule,
    UnifiedView,
)
from .util import canonical_digest


def layout_fingerprint(layout: LayoutPolicy) -> str:
    return canonical_digest(
        {
            "kind": "audit-mask-layout",
            "layout": layout.to_dict(),
        }
    )


def defaults_fingerprint(
    defaults: Dict[str, CategoryDefault],
) -> str:
    return canonical_digest(
        {
            "kind": "audit-mask-category-defaults",
            "defaults": {
                key: value.to_dict()
                for key, value in sorted(defaults.items())
            },
        }
    )


def _conflict_for_records(
    rule_name: str,
    generation: int,
    records: Sequence[RuleRecord],
) -> Optional[RuleConflict]:
    non_tombstones = [record for record in records if not record.tombstone]
    if len(non_tombstones) < 2:
        return None
    contents = {record.content_key for record in non_tombstones}
    if len(contents) <= 1:
        return None
    values: List[Dict[str, Any]] = []
    for record in sorted(
        non_tombstones,
        key=lambda item: (
            item.source_format,
            item.source_ref,
            item.record_index,
        ),
    ):
        values.append(
            {
                "source_format": record.source_format,
                "source_ref": record.source_ref,
                "record_index": record.record_index,
                "field_path": record.field_path,
                "mask": record.mask,
                "threshold": record.threshold,
                "effective_ms": record.effective_ms,
                "raw": record.raw,
            }
        )
    return RuleConflict(
        rule_name=rule_name,
        generation=generation,
        field_values=values,
    )


def build_unified_view(
    records: Iterable[RuleRecord],
    layout: LayoutPolicy,
    defaults: Optional[Dict[str, CategoryDefault]] = None,
) -> UnifiedView:
    defaults = defaults or {}
    grouped: Dict[str, List[RuleRecord]] = defaultdict(list)
    for record in records:
        grouped[record.rule_name].append(record)

    rules_by_name: Dict[str, UnifiedRule] = {}
    conflicts: List[RuleConflict] = []
    for rule_name in sorted(grouped):
        group = grouped[rule_name]
        by_generation: Dict[int, List[RuleRecord]] = defaultdict(list)
        for record in group:
            by_generation[record.generation].append(record)
        generation_conflicts: Dict[int, RuleConflict] = {}
        for generation in sorted(by_generation):
            conflict = _conflict_for_records(
                rule_name,
                generation,
                by_generation[generation],
            )
            if conflict:
                generation_conflicts[generation] = conflict
                conflicts.append(conflict)

        authority = sorted(
            group,
            key=lambda item: (
                item.generation,
                item.effective_ms,
                1 if item.tombstone else 0,
                item.source_format,
                item.source_ref,
                item.record_index,
            ),
            reverse=True,
        )[0]
        authority_conflict = generation_conflicts.get(authority.generation)
        if authority.tombstone:
            state = "tombstoned"
        elif authority_conflict is not None:
            state = "conflict"
        else:
            state = "active"

        conflict_for_rule = (
            authority_conflict
            if authority_conflict is not None
            else (
                next(iter(generation_conflicts.values()))
                if generation_conflicts
                else None
            )
        )
        rule = UnifiedRule(
            rule_name=rule_name,
            state=state,
            sources=sorted(
                group,
                key=lambda item: (
                    item.generation,
                    item.effective_ms,
                    item.source_format,
                    item.source_ref,
                    item.record_index,
                ),
            ),
            authority_source=authority,
            conflict=conflict_for_rule,
        )
        if state == "active":
            rule.field_path = (
                canonical_field_path(authority.field_path)
                if authority.field_path
                else None
            )
            rule.mask = authority.mask
            rule.threshold = authority.threshold
            rule.generation = authority.generation
            rule.effective_ms = authority.effective_ms
        else:
            rule.generation = authority.generation
            rule.effective_ms = authority.effective_ms
        rules_by_name[rule_name] = rule

    state_for_digest = {
        "kind": "audit-mask-unified-view",
        "layout": layout.to_dict(),
        "defaults": {
            key: value.to_dict() for key, value in sorted(defaults.items())
        },
        "rules": {
            name: rules_by_name[name].signature()
            for name in sorted(rules_by_name)
        },
        "conflicts": [conflict.to_dict() for conflict in conflicts],
    }
    fingerprint = canonical_digest(state_for_digest)
    return UnifiedView(
        layout=layout,
        defaults=defaults,
        rules_by_name=rules_by_name,
        conflicts=conflicts,
        fingerprint=fingerprint,
    )


def view_rules_fingerprint(view: UnifiedView) -> str:
    return view.fingerprint


def conflicting_rules_for_field(
    view: UnifiedView,
    field_name: str,
) -> List[UnifiedRule]:
    matches: List[UnifiedRule] = []
    for rule in view.rules_by_name.values():
        if rule.state != "conflict":
            continue
        if any(
            path and _path_matches(path, field_name)
            for path in rule.dependency_paths()
        ):
            matches.append(rule)
    return sorted(matches, key=lambda item: item.rule_name)


def _path_matches(rule_path: str, field_name: str) -> bool:
    from .masking import field_matches_path

    return field_matches_path(rule_path, field_name)


def active_rules_for_field(
    view: UnifiedView,
    field_name: str,
) -> List[UnifiedRule]:
    from .masking import field_matches_path

    return [
        rule
        for rule in view.active_rules
        if rule.field_path and field_matches_path(rule.field_path, field_name)
    ]


def rule_ranking_key(rule: UnifiedRule) -> Tuple[int, int, int, str]:
    return (
        -(rule.threshold if rule.threshold is not None else -1),
        -(rule.generation if rule.generation is not None else -1),
        -(rule.effective_ms if rule.effective_ms is not None else -1),
        rule.rule_name,
    )
