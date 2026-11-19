from __future__ import annotations

from multilabel_lab.hashing import canonical_json_bytes, sha256_json
from multilabel_lab.pipeline import classify_experiment
from multilabel_lab.verifier import verify_experiment


def test_same_snapshot_is_byte_identical(snapshot_factory):
    snapshot = snapshot_factory(sample_count=8)

    first = classify_experiment(snapshot)
    second = classify_experiment(snapshot)

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert sha256_json(first) == sha256_json(second)
    assert first["status"] == "ok"
    assert first["result_code"] == "OK"
    assert first["injection_records"] == []
    assert first["degradations"] == []
    assert first["metrics_path"]["mode"] == "normal"


def test_stage_audits_and_intermediate_hashes_are_complete(snapshot_factory):
    snapshot = snapshot_factory(sample_count=6)
    report = classify_experiment(snapshot)
    sample_ids = [sample["sample_id"] for sample in report["samples"]]

    assert len(sample_ids) == 6
    assert all(report["stage_audit"][stage] == sample_ids for stage in (
        "preprocess",
        "feature",
        "score",
        "decision",
    ))
    for sample in report["samples"]:
        assert len(sample["stages"]["preprocess"]["sha256"]) == 64
        assert len(sample["stages"]["feature"]["sha256"]) == 64
        assert len(sample["stages"]["score"]["sha256"]) == 64
        assert len(sample["stages"]["decision"]["sha256"]) == 64
        assert sample["stages"]["score"]["labels"] == report["config"]["labels"]
        assert len(sample["stages"]["score"]["values"]) == len(
            report["config"]["labels"]
        )


def test_verifier_passes_for_two_independent_runs(snapshot_factory):
    snapshot = snapshot_factory(sample_count=8)
    result = verify_experiment(snapshot)

    assert result["status"] == "ok"
    assert result["result_code"] == "OK"
    assert result["verification"]["matched"] is True
    assert result["verification"]["first_hash"] == result["verification"]["second_hash"]
    assert result["verification"]["first_deviation"] is None
    assert all(check["passed"] for check in result["verification"]["checks"])
