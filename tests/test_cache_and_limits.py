from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from coref_pipeline.errors import E_RESOURCE_LIMIT, PipelineError
from coref_pipeline.pipeline import run_pipeline

from .conftest import write_config, write_events


def _semantic(report: dict) -> dict:
    return {
        "chains": report["chains"],
        "evidence": report["evidence"],
        "arbitrations": report["arbitrations"],
        "consistency": report["consistency"],
        "incremental": {
            "state_hash": report["incremental"]["state_hash"],
            "batch_records": [
                {
                    key: value
                    for key, value in batch.items()
                    if key not in {"chain_cache_hit_ids", "chain_cache_miss_ids"}
                }
                for batch in report["incremental"]["batch_records"]
            ],
        },
    }


def test_cache_warm_hits_reuse_upstream_artifacts(sample_dir, tmp_path):
    cache_dir = tmp_path / "cache"
    first = run_pipeline(
        sample_dir=sample_dir, cache_dir=cache_dir, include_cache_trace=True
    )
    second = run_pipeline(
        sample_dir=sample_dir, cache_dir=cache_dir, include_cache_trace=True
    )
    assert first["cache_trace"]["affected_event_ids"]
    assert first["cache_trace"]["affected_evidence_pairs"]
    assert first["cache_trace"]["affected_chain_ids"]
    assert second["cache_trace"]["affected_event_ids"] == []
    assert second["cache_trace"]["reused_event_ids"]
    assert second["cache_trace"]["affected_evidence_pairs"] == []
    assert second["cache_trace"]["reused_evidence_pairs"]
    assert second["cache_trace"]["affected_chain_ids"] == []
    assert second["cache_trace"]["reused_chain_ids"]
    assert second["consistency"]["ok"]
    assert _semantic(first) == _semantic(second)


def test_event_correction_invalidates_only_affected_suffix(
    sample_dir, sample_events, tmp_path
):
    cache_dir = tmp_path / "cache"
    run_pipeline(sample_dir=sample_dir, cache_dir=cache_dir, include_cache_trace=True)

    mutated_dir = tmp_path / "mutated"
    shutil.copytree(sample_dir, mutated_dir)
    events = copy.deepcopy(sample_events)
    for event in events:
        if event["event_id"] == "ev-0003":
            for entity in event["entities"]:
                if entity["name"] == "Orion Cloud":
                    entity["role"] = "TOPIC"
    write_events(mutated_dir / "events.jsonl", events)

    cached = run_pipeline(
        sample_dir=mutated_dir,
        cache_dir=cache_dir,
        include_cache_trace=True,
    )
    trace = cached["cache_trace"]
    assert trace["affected_event_ids"] == ["ev-0003"]
    assert trace["reused_event_ids"]
    assert trace["affected_evidence_pairs"]
    assert all("ev-0003" in pair for pair in trace["affected_evidence_pairs"])
    assert trace["reused_evidence_pairs"]
    assert "chn_ev-0003" in trace["affected_chain_ids"]
    assert trace["reused_chain_ids"]
    assert cached["consistency"]["ok"]

    fresh = run_pipeline(sample_dir=mutated_dir)
    assert _semantic(cached) == _semantic(fresh)


def test_floor_and_trigger_version_invalidate_evidence(
    sample_dir, rules_payload, tmp_path
):
    cache_dir = tmp_path / "cache"
    run_pipeline(sample_dir=sample_dir, cache_dir=cache_dir)

    floor_payload = copy.deepcopy(rules_payload)
    floor_payload["candidate_floor"] = 0.7
    floor_path = write_config(tmp_path / "floor_rules.json", floor_payload)
    floor_report = run_pipeline(
        sample_dir=sample_dir,
        config_path=floor_path,
        cache_dir=cache_dir,
        include_cache_trace=True,
    )
    assert floor_report["cache_trace"]["affected_event_ids"] == []
    assert floor_report["cache_trace"]["reused_event_ids"]
    assert floor_report["cache_trace"]["affected_evidence_pairs"]
    assert floor_report["cache_trace"]["affected_chain_ids"]
    assert floor_report["consistency"]["ok"]

    trigger_payload = copy.deepcopy(rules_payload)
    trigger_payload["trigger_similarity"]["version"] = 2
    trigger_payload["trigger_similarity"]["method"] = "sequence_matcher"
    trigger_path = write_config(tmp_path / "trigger_rules.json", trigger_payload)
    trigger_report = run_pipeline(
        sample_dir=sample_dir,
        config_path=trigger_path,
        cache_dir=cache_dir,
        include_cache_trace=True,
    )
    assert trigger_report["cache_trace"]["affected_event_ids"] == []
    assert trigger_report["cache_trace"]["affected_evidence_pairs"]
    assert trigger_report["consistency"]["ok"]


def test_resource_limit_error_and_degrade(sample_dir, rules_payload, tmp_path):
    hard_payload = copy.deepcopy(rules_payload)
    hard_payload["resource_limits"]["max_pair_comparisons"] = 1
    hard_path = write_config(tmp_path / "hard.json", hard_payload)
    with pytest.raises(PipelineError) as exc:
        run_pipeline(sample_dir=sample_dir, config_path=hard_path)
    assert exc.value.code == E_RESOURCE_LIMIT
    assert exc.value.stage == "resource_limit"
    assert exc.value.field == "resource_limits.max_pair_comparisons"

    degraded_payload = copy.deepcopy(rules_payload)
    degraded_payload["resource_limits"]["max_pair_comparisons"] = 1
    degraded_payload["resource_limit_mode"] = "degrade"
    degraded_payload["degraded_candidate_floor"] = 0.9
    degraded_path = write_config(tmp_path / "degraded.json", degraded_payload)
    report = run_pipeline(sample_dir=sample_dir, config_path=degraded_path)
    assert report["resource"]["degraded"]
    assert report["resource"]["degradation_categories"] == ["higher_evidence_floor"]
    assert report["rules"]["effective_floor"] == 0.9
    assert all(
        record["score"] >= 0.9 for record in report["evidence"]["records"]
    )
    assert report["consistency"]["ok"]

    batch_payload = copy.deepcopy(rules_payload)
    batch_payload["resource_limits"]["max_batch_events"] = 2
    batch_path = write_config(tmp_path / "batch.json", batch_payload)
    with pytest.raises(PipelineError) as exc:
        run_pipeline(sample_dir=sample_dir, config_path=batch_path)
    assert exc.value.code == E_RESOURCE_LIMIT
    assert exc.value.field == "resource_limits.max_batch_events"
    assert "batch_no" in exc.value.details

    memory_payload = copy.deepcopy(rules_payload)
    memory_payload["resource_limits"]["max_resident_chain_mb"] = 0.000001
    memory_path = write_config(tmp_path / "memory.json", memory_payload)
    with pytest.raises(PipelineError) as exc:
        run_pipeline(sample_dir=sample_dir, config_path=memory_path)
    assert exc.value.code == E_RESOURCE_LIMIT
    assert exc.value.field == "resource_limits.max_resident_chain_mb"
    assert exc.value.event_id
