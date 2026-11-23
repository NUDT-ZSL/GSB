from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coref_pipeline.config import RulesConfig
from coref_pipeline.errors import (
    E_DUP_EVENT,
    E_EVIDENCE_CONFIG,
    E_OFFSET_INVALID,
    E_ROLE_INVALID,
    E_TYPE_INVALID,
    PipelineError,
)
from coref_pipeline.evidence import recompute_score
from coref_pipeline.ingest import ingest_events, read_controlled_set
from coref_pipeline.pipeline import run_pipeline

from .conftest import write_config


def _ingest(sample_dir: Path, raw_events: list[dict]):
    config = RulesConfig.load(sample_dir / "coref_rules.json")
    return ingest_events(
        raw_events,
        config=config,
        controlled_event_types=read_controlled_set(
            sample_dir / "event_types.json", "event_types"
        ),
        controlled_roles=read_controlled_set(
            sample_dir / "entity_dictionary.json", "roles"
        ),
    )


def test_normalization_is_sorted_and_deterministic(sample_dir, sample_events):
    forward = _ingest(sample_dir, sample_events)
    reverse = _ingest(sample_dir, list(reversed(sample_events)))
    assert forward.events == reverse.events
    assert forward.event_digest == reverse.event_digest
    keys = [(event["batch_no"], event["event_id"]) for event in forward.events]
    assert keys == sorted(keys)
    for event in forward.events:
        assert event["entities"] == sorted(
            event["entities"], key=lambda item: (item["role"], item["name"])
        )
        assert event["mentions"] == sorted(
            event["mentions"],
            key=lambda item: (item["start"], item["end"], item["trigger"]),
        )


@pytest.mark.parametrize(
    ("mutator", "code", "field"),
    [
        (
            lambda events: events.append(copy.deepcopy(events[0])),
            E_DUP_EVENT,
            "event_id",
        ),
        (
            lambda events: events[0].__setitem__("event_type", "NOT_A_TYPE"),
            E_TYPE_INVALID,
            "event_type",
        ),
        (
            lambda events: events[0]["mentions"][0].__setitem__(
                "end", len(events[0]["mentions"][0]["trigger"]) + 1
            ),
            E_OFFSET_INVALID,
            "mentions[0].start/end",
        ),
        (
            lambda events: events[0]["entities"][0].__setitem__("role", "BAD_ROLE"),
            E_ROLE_INVALID,
            "entities[0].role",
        ),
    ],
)
def test_ingestion_business_errors(
    sample_dir, sample_events, mutator, code, field
):
    events = copy.deepcopy(sample_events)
    mutator(events)
    with pytest.raises(PipelineError) as exc:
        _ingest(sample_dir, events)
    assert exc.value.code == code
    assert exc.value.stage == "ingest"
    assert exc.value.field == field
    assert exc.value.event_id
    payload = exc.value.to_dict()
    assert payload["code"] == code
    assert payload["event_id"] == exc.value.event_id


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("weights", "entity"), -0.1),
        (("candidate_floor",), 9.0),
        (("trigger_similarity", "method"), "unsupported"),
        (("resource_limits", "max_batch_events"), 0),
    ],
)
def test_evidence_config_errors_are_attributed(
    tmp_path, rules_payload, path, value
):
    payload = copy.deepcopy(rules_payload)
    cursor = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    config_path = write_config(tmp_path / "bad_rules.json", payload)
    with pytest.raises(PipelineError) as exc:
        RulesConfig.load(config_path)
    assert exc.value.code == E_EVIDENCE_CONFIG
    assert exc.value.stage == "evidence_config"
    assert exc.value.field == ".".join(path)


def test_evidence_components_recompute_and_floor(sample_dir):
    report = run_pipeline(sample_dir=sample_dir)
    evidence_records = report["evidence"]["records"]
    assert evidence_records
    assert [(item["event_a"], item["event_b"]) for item in evidence_records] == sorted(
        (item["event_a"], item["event_b"]) for item in evidence_records
    )
    for evidence in evidence_records:
        result = recompute_score(evidence)
        assert result["ok"], result
        assert evidence["score"] >= evidence["score_floor"]
        assert set(evidence["components"]) == {
            "entity",
            "time",
            "trigger",
            "source_trust",
        }
        assert "a_mention" in evidence["components"]["trigger"]
        assert "b_mention" in evidence["components"]["trigger"]
        assert evidence["matched_entities"] is not None


def test_batch_order_is_effective_ascending(sample_dir):
    report = run_pipeline(sample_dir=sample_dir, batch_order="4,1,2")
    assert report["inputs"]["requested_batch_order"] == [1, 2, 4]
    assert report["inputs"]["effective_batch_order"] == [1, 2, 4]
    assert report["inputs"]["event_count"] < 14
    assert report["consistency"]["ok"]


def test_controlled_role_requires_weight(sample_dir, rules_payload, tmp_path):
    payload = copy.deepcopy(rules_payload)
    payload["entity_role_weights"].pop("LOCATION")
    config_path = write_config(tmp_path / "missing_role.json", payload)
    with pytest.raises(PipelineError) as exc:
        run_pipeline(sample_dir=sample_dir, config_path=config_path)
    assert exc.value.code == E_EVIDENCE_CONFIG
    assert exc.value.field == "entity_role_weights.LOCATION"
