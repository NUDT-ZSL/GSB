from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from firewall_audit.codes import (
    E_NO_MATCH,
    E_NOT_EQUIVALENT,
    E_RULE_CONFLICT,
    E_TOMBSTONED,
)
from firewall_audit.engine import adjudicate_bundle
from firewall_audit.parser import parse_sample


def test_equivalence_reasons_and_anomaly_codes(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    results, _ = adjudicate_bundle(bundle)

    assert results["case-shared-web"].equivalent is True
    assert results["case-shared-web"].reason == "same_rule_key"
    assert results["case-shared-web"].v1.rule_key == "shared-web"
    assert results["case-shared-web"].v2.rule_key == "shared-web"
    assert results["case-shared-web"].v1.basis_generation == 1
    assert results["case-shared-web"].v2.basis_generation == 2

    assert results["case-alias-dns"].equivalent is True
    assert results["case-alias-dns"].reason == "mapping_covered"
    assert results["case-alias-dns"].v1.rule_key == "v1-alias-dns"
    assert results["case-alias-dns"].v2.rule_key == "v2-alias-dns"

    assert results["case-semantic"].equivalent is True
    assert results["case-semantic"].reason == "same_semantics"
    assert results["case-semantic"].v1.rule_key != results["case-semantic"].v2.rule_key

    assert results["case-none"].equivalent is True
    assert results["case-none"].v1.code == E_NO_MATCH
    assert results["case-none"].v2.code == E_NO_MATCH

    tombstone = results["case-tombstone"]
    assert tombstone.equivalent is False
    assert tombstone.code == E_NOT_EQUIVALENT
    assert tombstone.v1.status == "hit"
    assert tombstone.v2.status == "tombstoned"
    assert tombstone.v2.code == E_TOMBSTONED
    assert tombstone.v2.rule_key == "shared-retired"
    assert tombstone.v2.basis_generation == 3
    assert tombstone.action_diff is not None

    assert results["case-v1-legacy"].equivalent is False
    assert results["case-v2-extra"].equivalent is False
    assert results["case-ambiguous"].equivalent is False
    assert results["case-ambiguous"].v2.code == E_RULE_CONFLICT


def test_same_sequence_conflict_and_policy(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    strict_results, _ = adjudicate_bundle(bundle, case_ids=["case-conflict"])
    strict = strict_results["case-conflict"]
    assert strict.v1.code == E_RULE_CONFLICT
    assert strict.v2.code == E_RULE_CONFLICT
    assert len(strict.v1.hits) == 2
    assert strict.equivalent is True
    assert strict.reason == "mapping_covered"
    assert strict.code == E_RULE_CONFLICT

    deny_bundle = replace(bundle, conflict_policy="deny_wins")
    deny_results, _ = adjudicate_bundle(deny_bundle, case_ids=["case-conflict"])
    deny = deny_results["case-conflict"]
    assert deny.v1.status == "hit"
    assert deny.v1.action == "deny"
    assert deny.v2.status == "hit"
    assert deny.v2.action == "deny"


def test_independent_side_views_and_unified_view(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    results, _ = adjudicate_bundle(bundle)
    tombstone = results["case-tombstone"]
    assert tombstone.v1.status == "hit"
    assert tombstone.v2.status == "tombstoned"
    assert tombstone.unified.status == "tombstoned"
    assert tombstone.unified.basis_generation == 3

    legacy = results["case-v1-legacy"]
    assert legacy.v1.status == "hit"
    assert legacy.v2.status == "no_match"
    assert legacy.unified.status == "hit"


def test_concurrency_matches_serial(sample_manifest: Path) -> None:
    bundle = parse_sample(sample_manifest)
    serial, _ = adjudicate_bundle(bundle, jobs=1)
    parallel, _ = adjudicate_bundle(bundle, jobs=4)
    assert list(serial) == list(parallel)
    for case_id in serial:
        assert serial[case_id].to_dict() == parallel[case_id].to_dict()
