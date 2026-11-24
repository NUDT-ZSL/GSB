from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from smugglebench.codes import E_RECOMPUTE_DIVERGE, E_RULE_CYCLE
from smugglebench.incremental import incremental_recompute
from smugglebench.normalize import load_workspace
from smugglebench.util import BusinessError


def test_tombstone_patch_recomputes_only_closure_and_matches_full_replay(
    sample_workspace: Path,
    baseline_report: dict,
) -> None:
    data = load_workspace(sample_workspace)
    source = next(
        rule
        for rule in data.rules
        if rule.canonical_name == "te-cl-observation" and rule.generation == 2
    )
    tombstone = dataclasses.replace(
        source,
        generation=3,
        effective_from_ms=data.observed_clock_ms,
        enabled=False,
        tombstone=True,
        source_file="<patch>",
        record_index=0,
    )
    outcome = incremental_recompute(
        data,
        baseline_report,
        [*data.rules, tombstone],
    )
    assert outcome.changed_rules == ["te-cl-observation"]
    assert outcome.affected_connections == ["conn-tecl", "conn-trailer"]
    assert outcome.affected_mutations == [
        "m01-te-cl-conflict",
        "m04-trailer-injection",
    ]
    assert outcome.verification == "EQUAL_TO_FULL_REPLAY"
    patched = {item["mutation_id"]: item for item in outcome.report["mutations"]}
    assert not any(
        hit["rule_key"] == "te-cl-observation"
        for hit in patched["m01-te-cl-conflict"]["rule_hits"]
    )
    assert patched["m04-trailer-injection"]["rule_hits"]


def test_dependency_cycle_returns_business_code(
    sample_workspace: Path,
    baseline_report: dict,
) -> None:
    data = load_workspace(sample_workspace)
    patched = [
        dataclasses.replace(
            rule,
            depends_on=("trailer-injection",)
            if rule.canonical_name == "te-cl-observation"
            else rule.depends_on,
        )
        for rule in data.rules
    ]
    with pytest.raises(BusinessError) as raised:
        incremental_recompute(data, baseline_report, patched)
    assert raised.value.code == E_RULE_CYCLE
    assert set(raised.value.details["rule_names"]) == {
        "te-cl-observation",
        "trailer-injection",
    }
