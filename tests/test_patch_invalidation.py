from __future__ import annotations

import json
from pathlib import Path

import pytest

from alertlab.errors import AlertLabError
from alertlab.service import AlertLabService
from alertlab.util import canonical_json

from conftest import ROOT, read_json


def summary_only(cluster: dict) -> dict:
    return {key: value for key, value in cluster.items() if key != "root_candidates"}


def test_dependency_patch_reuses_aggregation_and_keeps_unaffected_rows_exact(
    loaded_service: AlertLabService,
) -> None:
    before = loaded_service.status()
    patch = read_json(ROOT / "samples" / "patches" / "dependency_patch.json")
    result = loaded_service.patch(patch)
    after = loaded_service.status()
    assert result["applied"] is True
    assert result["rule_view"]["kind"] == "patch"
    gateway_before = next(
        cluster for cluster in before["clusters"] if cluster["rule_name"] == "GatewayLatency"
    )
    gateway_after = next(
        cluster for cluster in after["clusters"] if cluster["rule_name"] == "GatewayLatency"
    )
    assert summary_only(gateway_before) == summary_only(gateway_after)
    assert gateway_before["root_candidates"] != gateway_after["root_candidates"]
    assert {candidate["service"] for candidate in gateway_after["root_candidates"]} == {
        "edge-gateway",
        "auth-api",
    }
    for old in before["clusters"]:
        if old["rule_name"] == "GatewayLatency":
            continue
        new = next(cluster for cluster in after["clusters"] if cluster["cluster_id"] == old["cluster_id"])
        assert old == new
    assert gateway_after["cluster_id"] in result["reused"]["clusters"]
    assert result["invalidated"]["root_candidates"]
    paths = {item["path"] for item in result["invalidations"]}
    assert any(path.startswith("rule:GatewayLatency") for path in paths)
    assert any("/candidate:" in path for path in paths)
    assert any(
        item["action"] == "reuse_summary_recompute_candidates"
        for item in result["invalidated"]["clusters"]
    )
    assert loaded_service.verify()["ok"] is True


def test_patch_is_idempotent_and_conflicts_are_rejected(
    loaded_service: AlertLabService,
) -> None:
    patch = read_json(ROOT / "samples" / "patches" / "dependency_patch.json")
    first = loaded_service.patch(patch)
    status = loaded_service.status()
    second = loaded_service.patch(patch)
    assert second["idempotent"] is True
    assert second["rule_view"]["view_id"] == first["rule_view"]["view_id"]
    assert loaded_service.status() == status

    conflicting = dict(patch)
    conflicting["changes"] = [
        {
            "rule_name": "GatewayLatency",
            "set": {"silence_labels": ["other"]},
        }
    ]
    with pytest.raises(AlertLabError) as captured:
        loaded_service.patch(conflicting)
    assert captured.value.code == "E_PATCH_CONFLICT"


def test_effective_time_patch_supersedes_clusters(
    loaded_service: AlertLabService,
) -> None:
    before = loaded_service.status()
    patch = {
        "patch_id": "effective-late",
        "logical_clock_ms": 1_700_002_200_000,
        "changes": [
            {
                "rule_name": "GatewayLatency",
                "set": {"effective_at_ms": 1_700_001_200_000},
            }
        ],
    }
    result = loaded_service.patch(patch)
    after = loaded_service.status()
    assert len(after["clusters"]) == len(before["clusters"]) - 1
    assert all(cluster["rule_name"] != "GatewayLatency" for cluster in after["clusters"])
    superseded = [
        item
        for item in result["invalidated"]["clusters"]
        if item["rule_name"] == "GatewayLatency"
    ]
    assert superseded
    assert all(item["action"] == "supersede" for item in superseded)
    assert loaded_service.verify()["ok"] is True


def test_combined_patch_recomputes_silence_effective_and_dependencies(
    loaded_service: AlertLabService,
) -> None:
    before = loaded_service.status()
    patch = read_json(ROOT / "samples" / "patches" / "combined_patch.json")
    result = loaded_service.patch(patch)
    after = loaded_service.status()
    gateway = next(
        cluster for cluster in after["clusters"] if cluster["rule_name"] == "GatewayLatency"
    )
    assert "degraded" in gateway["rule_silence_labels"]
    assert gateway["rule_effective_at_ms"] == 1_700_001_000_000
    assert {candidate["service"] for candidate in gateway["root_candidates"]} == {
        "edge-gateway",
        "order-api",
        "payment-db",
    }
    for old in before["clusters"]:
        if old["rule_name"] == "GatewayLatency":
            continue
        new = next(cluster for cluster in after["clusters"] if cluster["cluster_id"] == old["cluster_id"])
        assert old == new
    assert loaded_service.verify()["ok"] is True


def test_invalid_patch_does_not_advance_view_pointer(
    loaded_service: AlertLabService,
) -> None:
    before = loaded_service.status()
    invalid = {
        "patch_id": "invalid-dependency",
        "logical_clock_ms": 1_700_002_300_000,
        "changes": [
            {
                "rule_name": "GatewayLatency",
                "set": {"upstream_dependencies": ["missing-service"]},
            }
        ],
    }
    with pytest.raises(AlertLabError) as captured:
        loaded_service.patch(invalid)
    assert captured.value.code == "E_PATCH_DEPENDENCY_UNRESOLVED"
    assert loaded_service.status() == before
