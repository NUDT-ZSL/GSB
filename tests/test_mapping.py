from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from firewall_audit.codes import (
    E_MAP_AMBIGUOUS,
    E_MAP_CYCLE,
    E_MAP_EXTRA,
    E_MAP_MISSING,
)
from firewall_audit.mapping import reconcile_mapping
from firewall_audit.models import MappingEntry
from firewall_audit.parser import parse_sample


def _resolve(bundle, entries):
    return reconcile_mapping(
        bundle.rules,
        entries,
        declaration_generation=bundle.mapping_generation,
        conflict_policy=bundle.conflict_policy,
        direction=bundle.mapping_direction,
    )


def test_bidirectional_reconciliation_codes(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    result = _resolve(bundle, bundle.mapping_entries)
    codes = {issue.code for issue in result.issues}
    assert codes == {E_MAP_MISSING, E_MAP_EXTRA, E_MAP_AMBIGUOUS}
    missing = next(issue for issue in result.issues if issue.code == E_MAP_MISSING)
    extra = next(issue for issue in result.issues if issue.code == E_MAP_EXTRA)
    ambiguous = next(
        issue for issue in result.issues if issue.code == E_MAP_AMBIGUOUS
    )
    assert "v1-semantic-only" in missing.rule_keys
    assert "v1-legacy-only" in missing.rule_keys
    assert "shared-retired" not in missing.rule_keys
    assert "v2-extra-only" in extra.rule_keys
    assert "v2-semantic-only" in extra.rule_keys
    assert {"v1-ambig", "v2-ambig-a", "v2-ambig-b"} <= set(
        ambiguous.rule_keys
    )


def test_mapping_resolution_is_arrival_order_independent(
    sample_manifest: Path,
) -> None:
    bundle = parse_sample(sample_manifest)
    entries = list(bundle.mapping_entries)
    shuffled = [
        replace(entry, record_index=index)
        for index, entry in enumerate(reversed(entries), start=1)
    ]
    forward = _resolve(bundle, entries)
    backward = _resolve(bundle, shuffled)
    assert forward.pairs == backward.pairs
    assert [issue.code for issue in forward.issues] == [
        issue.code for issue in backward.issues
    ]
    assert [issue.rule_keys for issue in forward.issues] == [
        issue.rule_keys for issue in backward.issues
    ]


def test_mapping_generation_supersedes_and_withdraws(
    sample_manifest: Path,
) -> None:
    bundle = parse_sample(sample_manifest)
    entries = [
        MappingEntry("v1-alias-dns", "v2-semantic-only", 1, 1),
        MappingEntry("v1-alias-dns", "v2-alias-dns", 2, 2),
    ]
    result = _resolve(bundle, entries)
    assert result.pairs == (("v1-alias-dns", "v2-alias-dns"),)

    withdrawal = [
        *entries,
        MappingEntry("v1-alias-dns", "v2-alias-dns", 3, 3, active=False),
    ]
    withdrawn = _resolve(bundle, withdrawal)
    assert withdrawn.pairs == ()


def test_mapping_cycle_is_reported(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    entries = [
        MappingEntry("shared-web", "shared-retired", 3, 1),
        MappingEntry("shared-retired", "shared-web", 3, 2),
    ]
    result = _resolve(bundle, entries)
    assert result.cycle_rule_keys == ("shared-retired", "shared-web")
    assert E_MAP_CYCLE in {issue.code for issue in result.issues}


def test_unknown_mapping_keys_are_missing_or_extra(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    entries = [
        MappingEntry("ghost-v1", "shared-web", 3, 1),
        MappingEntry("shared-web", "ghost-v2", 3, 2),
    ]
    result = _resolve(bundle, entries)
    issues = {issue.code: issue for issue in result.issues}
    assert "ghost-v1" in issues[E_MAP_MISSING].rule_keys
    assert "ghost-v2" in issues[E_MAP_EXTRA].rule_keys


def test_many_to_one_is_reported_separately(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    entries = [
        MappingEntry("v1-alias-dns", "v2-semantic-only", 3, 1),
        MappingEntry("v1-semantic-only", "v2-semantic-only", 3, 2),
    ]
    result = _resolve(bundle, entries)
    ambiguous = [
        issue for issue in result.issues if issue.code == E_MAP_AMBIGUOUS
    ]
    assert len(ambiguous) == 1
    assert "mapped from multiple v1 rules" in ambiguous[0].message
    assert set(ambiguous[0].rule_keys) == {
        "v1-alias-dns",
        "v1-semantic-only",
        "v2-semantic-only",
    }
