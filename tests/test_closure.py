from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from firewall_audit.closure import compute_closure
from firewall_audit.codes import E_MAP_CYCLE
from firewall_audit.engine import adjudicate_bundle
from firewall_audit.models import MappingEntry
from firewall_audit.parser import (
    apply_mapping_patch,
    parse_mapping_declaration,
    parse_sample,
)


def test_mapping_patch_only_recomputes_affected_cases(
    sample_manifest: Path,
    incremental_manifest: Path,
) -> None:
    base = parse_sample(sample_manifest)
    patched = parse_sample(incremental_manifest)
    base_results, _ = adjudicate_bundle(base)
    closure = compute_closure(base, patched, base_results)
    assert closure["affected_cases"] == ["case-semantic"]
    assert closure["removed_cases"] == []
    assert closure["snapshot_replacement"]["replacement"] is True
    assert "mapping_generation_changed" in closure["snapshot_replacement"]["reasons"]

    incremental, _ = adjudicate_bundle(
        patched, case_ids=closure["affected_cases"]
    )
    merged = dict(base_results)
    merged.update(incremental)
    full, _ = adjudicate_bundle(patched)
    assert sorted(merged) == sorted(full)
    for case_id in full:
        assert merged[case_id].to_dict() == full[case_id].to_dict()


def test_changed_rule_recomputes_only_its_cases(sample_manifest: Path) -> None:
    base = parse_sample(sample_manifest)
    base_results, _ = adjudicate_bundle(base)
    changed_rules = tuple(
        replace(rule, action="deny")
        if rule.side == "v2" and rule.rule_key == "shared-web"
        else rule
        for rule in base.rules
    )
    changed = replace(base, rules=changed_rules)
    closure = compute_closure(base, changed, base_results)
    assert closure["affected_cases"] == ["case-shared-web"]


def test_reordering_does_not_create_replacement_or_work(
    sample_manifest: Path,
) -> None:
    base = parse_sample(sample_manifest)
    base_results, _ = adjudicate_bundle(base)
    reordered = replace(
        base,
        rules=tuple(reversed(base.rules)),
        mapping_entries=tuple(reversed(base.mapping_entries)),
    )
    assert base.rule_set_fingerprint() == reordered.rule_set_fingerprint()
    assert base.mapping_fingerprint() == reordered.mapping_fingerprint()
    closure = compute_closure(base, reordered, base_results)
    assert closure["snapshot_replacement"]["replacement"] is False
    assert closure["affected_cases"] == []


def test_conflict_policy_change_only_invalidates_ties(sample_manifest: Path) -> None:
    base = parse_sample(sample_manifest)
    base_results, _ = adjudicate_bundle(base)
    changed = replace(base, conflict_policy="deny_wins")
    closure = compute_closure(base, changed, base_results)
    assert set(closure["affected_cases"]) == {
        "case-ambiguous",
        "case-conflict",
        "case-semantic",
    }


def test_direct_mapping_patch_merges_and_invalidates_one_case(
    sample_manifest: Path,
    generated_sample: Path,
) -> None:
    base = parse_sample(sample_manifest)
    base_results, _ = adjudicate_bundle(base)
    patch = parse_mapping_declaration(
        generated_sample / "incremental" / "mapping-patch-v4.json"
    )
    patched = apply_mapping_patch(base, patch)
    assert patched.mapping_generation == 4
    patched_again = apply_mapping_patch(patched, patch)
    assert patched_again.mapping_entries == patched.mapping_entries
    assert patched_again.mapping_fingerprint() == patched.mapping_fingerprint()
    closure = compute_closure(base, patched, base_results)
    assert closure["affected_cases"] == ["case-semantic"]


def test_cycle_closure_is_sorted_and_reported(sample_manifest: Path) -> None:
    base = parse_sample(sample_manifest)
    base_results, _ = adjudicate_bundle(base)
    cyclic_entries = (
        MappingEntry("shared-web", "shared-retired", 3, 1),
        MappingEntry("shared-retired", "shared-web", 3, 2),
    )
    cyclic = replace(
        base,
        mapping_entries=cyclic_entries,
        mapping_generation=4,
    )
    closure = compute_closure(base, cyclic, base_results)
    assert E_MAP_CYCLE in closure["codes"]
    assert closure["affected_cases"] == sorted(closure["affected_cases"])
    assert {
        "case-shared-web",
        "case-tombstone",
    } <= set(closure["affected_cases"])
