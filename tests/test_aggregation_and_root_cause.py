from __future__ import annotations

import json

from alertlab.service import AlertLabService
from alertlab.util import canonical_json


def test_clusters_are_deterministic_and_late_event_is_isolated(
    loaded_service: AlertLabService,
) -> None:
    first = loaded_service.status()
    second = loaded_service.status()
    assert canonical_json(first) == canonical_json(second)
    assert first["summary"]["cluster_count"] == 6
    assert first["summary"]["late_event_count"] == 1
    assert [event["seq"] for event in first["late_events"]] == [9]
    gateway = next(
        cluster
        for cluster in first["clusters"]
        if cluster["rule_name"] == "GatewayLatency"
    )
    assert gateway["representative"]["seq"] == 1
    assert gateway["occurrence_count"] == 3
    assert gateway["suppressed_count"] == 2
    assert gateway["silence_suppressed_count"] == 1
    status_gateway = next(
        cluster
        for cluster in first["clusters"]
        if cluster["rule_name"] == "GatewayLatency"
    )
    assert status_gateway["root_candidates"]
    late_uid = first["late_events"][0]["event_uid"]
    assert late_uid not in {
        candidate_signal["event_uid"]
        for candidate in status_gateway["root_candidates"]
        for candidate_signal in candidate["signals"]
    }


def test_repeated_load_does_not_duplicate_suppression_or_change_representative(
    loaded_service: AlertLabService,
) -> None:
    before = loaded_service.status()
    loaded_service.load()
    after = loaded_service.status()
    assert after == before
    assert after["summary"]["cluster_count"] == 6
    gateway_before = next(
        cluster for cluster in before["clusters"] if cluster["rule_name"] == "GatewayLatency"
    )
    gateway_after = next(
        cluster for cluster in after["clusters"] if cluster["rule_name"] == "GatewayLatency"
    )
    assert gateway_after == gateway_before
    assert gateway_after["suppressed_count"] == 2


def test_root_candidates_have_fixed_sort_and_evidence(
    loaded_service: AlertLabService,
) -> None:
    status = loaded_service.status()
    gateway = next(
        cluster
        for cluster in status["clusters"]
        if cluster["rule_name"] == "GatewayLatency"
    )
    candidates = gateway["root_candidates"]
    assert [candidate["rank"] for candidate in candidates] == list(
        range(1, len(candidates) + 1)
    )
    keys = [
        (
            0 if candidate["has_signal"] else 1,
            candidate["time_diff_ms"]
            if candidate["time_diff_ms"] is not None
            else float("inf"),
            -candidate["weight"],
            candidate["dependency_distance"],
            candidate["service"],
            candidate["instance_id"],
        )
        for candidate in candidates
    ]
    assert keys == sorted(keys)
    assert all(candidate["dependency_path"] for candidate in candidates)
    assert all("participating_instances" in candidate for candidate in candidates)
    assert all("signals" in candidate for candidate in candidates)
    top = candidates[0]
    assert top["candidate_id"] == "edge-gateway/gw-1"
    assert top["time_diff_ms"] == 0
    assert top["evidence"]["affected_instances"] == ["gw-1", "gw-2"]
    assert top["evidence"]["dependency_path"] == ["edge-gateway"]


def test_verify_replays_clusters_and_candidates(
    loaded_service: AlertLabService,
) -> None:
    result = loaded_service.verify()
    assert result["ok"] is True
    assert result["mismatches"] == []
