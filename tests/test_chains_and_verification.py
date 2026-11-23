from __future__ import annotations

import copy
import json

import pytest

from coref_pipeline.canonical import pretty_json
from coref_pipeline.chains import validate_chain_state
from coref_pipeline.errors import (
    E_ARBITRATION_UNDECIDED,
    E_LINK_CONFLICT,
    E_ORPHAN_EVENT,
    PipelineError,
)
from coref_pipeline.pipeline import run_pipeline
from coref_pipeline.verify import verify_report

from .conftest import write_events


def test_incremental_reuse_conflicts_and_argument_resolution(sample_dir):
    report = run_pipeline(sample_dir=sample_dir)
    assert report["status"] == "ok"
    assert report["consistency"]["ok"]
    assert report["verification"]["ok"]

    member_lists = [chain["events"] for chain in report["chains"]]
    flattened = [event_id for members in member_lists for event_id in members]
    assert len(flattened) == len(set(flattened)) == 14
    for chain in report["chains"]:
        assert chain["events"] == sorted(chain["events"], key=lambda eid: (
            next(
                event["batch_no"]
                for event in report["normalization"]["events"]
                if event["event_id"] == eid
            ),
            eid,
        ))

    chain_conflicts = [
        record
        for record in report["arbitrations"]
        if record["kind"] == "chain_conflict"
    ]
    assert chain_conflicts
    conflict = next(
        record for record in chain_conflicts if record["event_id"] == "ev-0009"
    )
    assert conflict["winner_chain_id"] == "chn_ev-0001"
    assert conflict["rejected_directions"]
    assert conflict["rejected_directions"][0]["reason_metric"] in {
        "evidence_total",
        "chain_size",
        "first_seen",
    }

    argument_conflicts = [
        record
        for record in report["arbitrations"]
        if record["kind"] == "argument_conflict"
    ]
    assert argument_conflicts
    product = next(
        record
        for record in argument_conflicts
        if record["chain_id"] == "chn_ev-0001" and record["role"] == "PRODUCT"
    )
    assert product["winner"] == "星云一号"
    assert product["losers"]
    assert all(loser["support"] for loser in product["losers"])

    for batch in report["incremental"]["batch_records"]:
        assert set(batch["untouched_chain_ids"]) <= set(batch["reused_chain_ids"])
        assert not (
            set(batch["recomputed_chain_ids"]) & set(batch["reused_chain_ids"])
        )
    batch3 = next(
        batch
        for batch in report["incremental"]["batch_records"]
        if batch["batch_no"] == 3
    )
    final_marker_chain = next(
        chain for chain in report["chains"] if chain["chain_id"] == "chn_ev-0007"
    )
    assert "chn_ev-0007" in batch3["reused_chain_ids"]
    assert (
        batch3["reused_state_hashes"]["chn_ev-0007"]
        == final_marker_chain["state_hash"]
    )

    assert sorted(tuple(chain["events"]) for chain in report["chains"]) == sorted(
        tuple(chain["events"]) for chain in report["full_rebuild"]["chains"]
    )
    assert report["arbitrations"] == report["full_rebuild"]["arbitrations"]


def test_report_is_byte_deterministic(sample_dir):
    first = run_pipeline(sample_dir=sample_dir)
    second = run_pipeline(sample_dir=sample_dir)
    assert pretty_json(first) == pretty_json(second)


def test_verifier_detects_tampered_chain(sample_dir):
    report = run_pipeline(sample_dir=sample_dir)
    tampered = copy.deepcopy(report)
    tampered["chains"][0]["events"] = tampered["chains"][0]["events"][:-1]
    result = verify_report(tampered)
    assert not result["ok"]
    assert result["errors"][0]["code"] == "E_INCONSISTENT"


def test_chain_state_validator_errors(sample_dir):
    report = run_pipeline(sample_dir=sample_dir)
    events = {
        event["event_id"]: event for event in report["normalization"]["events"]
    }
    evidence = {
        record["evidence_id"]: record for record in report["evidence"]["records"]
    }
    chains = copy.deepcopy(report["chains"])

    duplicate = copy.deepcopy(chains)
    duplicate_chain = copy.deepcopy(duplicate[0])
    duplicate_chain["chain_id"] = "chn_duplicate"
    duplicate.append(duplicate_chain)
    with pytest.raises(PipelineError) as exc:
        validate_chain_state(
            duplicate, events_by_id=events, evidence_by_id=evidence
        )
    assert exc.value.code == E_LINK_CONFLICT

    repeated_in_one_chain = copy.deepcopy(chains)
    repeated_in_one_chain[0]["events"].insert(1, repeated_in_one_chain[0]["events"][0])
    with pytest.raises(PipelineError) as exc:
        validate_chain_state(
            repeated_in_one_chain, events_by_id=events, evidence_by_id=evidence
        )
    assert exc.value.code == E_LINK_CONFLICT

    orphan = copy.deepcopy(chains)
    orphan[0]["events"].append("ev-missing")
    with pytest.raises(PipelineError) as exc:
        validate_chain_state(
            orphan, events_by_id=events, evidence_by_id=evidence
        )
    assert exc.value.code == E_ORPHAN_EVENT


def test_undecided_argument_arbitration(
    sample_dir, sample_events, tmp_path
):
    events = copy.deepcopy(sample_events)
    for event in events:
        if event["event_id"] == "ev-0002":
            event["entities"] = [
                {"name": "Nova Robotics", "role": "ORG"},
                {"name": "星云一号", "role": "PRODUCT"},
            ]
            event["source_trust"] = 0.95
    write_events(tmp_path / "events.jsonl", events)
    for name in ("event_types.json", "entity_dictionary.json", "coref_rules.json"):
        (tmp_path / name).write_bytes((sample_dir / name).read_bytes())
    with pytest.raises(PipelineError) as exc:
        run_pipeline(sample_dir=tmp_path)
    assert exc.value.code == E_ARBITRATION_UNDECIDED
    assert exc.value.stage == "argument_arbitration"
    assert "PRODUCT" in exc.value.field
    assert exc.value.details["conflicts"]
