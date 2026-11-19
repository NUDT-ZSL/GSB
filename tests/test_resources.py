from __future__ import annotations

from multilabel_lab.pipeline import classify_experiment
from multilabel_lab.verifier import verify_experiment


def test_sample_limit_truncates_and_marks_degraded_path(snapshot_factory):
    snapshot = snapshot_factory(
        sample_count=12,
        max_samples=5,
        sample_limit_policy="truncate",
    )
    report = classify_experiment(snapshot)

    assert report["status"] == "degraded"
    assert report["result_code"] == "OK_DEGRADED"
    assert report["business_codes"] == ["E_RESOURCE_LIMIT"]
    assert len(report["samples"]) == 5
    assert report["resource_events"][0]["resource"] == "sample_count"
    assert report["degradations"][0]["category"] == "DEGRADE_SAMPLE_TRUNCATION"
    assert report["metrics_path"]["mode"] == "degraded"
    assert verify_experiment(snapshot)["verification"]["result_code"] == "OK"


def test_feature_limit_truncates_projection_and_features(snapshot_factory):
    snapshot = snapshot_factory(
        sample_count=4,
        max_feature_dim=20,
        feature_limit_policy="truncate",
    )
    report = classify_experiment(snapshot)

    assert report["status"] == "degraded"
    assert report["resource_limits"]["effective_feature_dim"] == 20
    assert all(
        sample["stages"]["feature"]["dimension"] == 20
        for sample in report["samples"]
    )
    categories = {
        record["category"] for record in report["degradations"]
    }
    assert "DEGRADE_FEATURE_TRUNCATION" in categories


def test_memory_limit_degrades_by_truncating_samples(snapshot_factory):
    snapshot = snapshot_factory(
        sample_count=8,
        max_resident_matrix_bytes=12_000,
        memory_limit_policy="truncate_samples",
    )
    report = classify_experiment(snapshot)

    assert report["status"] == "degraded"
    assert len(report["samples"]) == 2
    assert report["resource_limits"]["estimated_resident_matrix_bytes"] <= 12_000
    assert any(
        record["category"] == "DEGRADE_MEMORY_SAMPLE_TRUNCATION"
        for record in report["degradations"]
    )


def test_resource_limit_with_error_policy_returns_business_code(snapshot_factory):
    snapshot = snapshot_factory(
        sample_count=8,
        max_samples=3,
        sample_limit_policy="error",
    )
    report = classify_experiment(snapshot)

    assert report["status"] == "error"
    assert report["result_code"] == "E_RESOURCE_LIMIT"
    assert report["business_codes"] == ["E_RESOURCE_LIMIT"]
    assert "metrics" not in report

    verified = verify_experiment(snapshot)
    assert verified["result_code"] == "E_RESOURCE_LIMIT"
    assert verified["verification"]["result_code"] == "OK"
    assert verified["verification"]["matched"] is True
