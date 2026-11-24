"""Canonical rule views for each generation and the merged view."""

from __future__ import annotations

from collections.abc import Iterable

from .models import NormalizedRule, RuleView


def _winner(records: Iterable[NormalizedRule]) -> NormalizedRule:
    return max(records, key=lambda rule: rule.authority_key())


def build_side_view(side: str, rules: Iterable[NormalizedRule]) -> RuleView:
    grouped: dict[str, list[NormalizedRule]] = {}
    for rule in rules:
        if rule.side == side:
            grouped.setdefault(rule.rule_key, []).append(rule)
    active: dict[str, NormalizedRule] = {}
    shadow: dict[str, NormalizedRule] = {}
    for key in sorted(grouped):
        winner = _winner(grouped[key])
        if winner.tombstone:
            shadow[key] = winner
        else:
            active[key] = winner
    return RuleView(side=side, active=active, shadow=shadow)


def build_unified_view(rules: Iterable[NormalizedRule]) -> RuleView:
    grouped: dict[str, list[NormalizedRule]] = {}
    for rule in rules:
        grouped.setdefault(rule.rule_key, []).append(rule)
    active: dict[str, NormalizedRule] = {}
    shadow: dict[str, NormalizedRule] = {}
    for key in sorted(grouped):
        winner = _winner(grouped[key])
        if winner.tombstone:
            shadow[key] = winner
        else:
            active[key] = winner
    return RuleView(side="unified", active=active, shadow=shadow)


def build_views(
    rules: Iterable[NormalizedRule],
) -> tuple[RuleView, RuleView, RuleView]:
    rule_tuple = tuple(rules)
    return (
        build_side_view("v1", rule_tuple),
        build_side_view("v2", rule_tuple),
        build_unified_view(rule_tuple),
    )
