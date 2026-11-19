from __future__ import annotations

from multilabel_lab.hashing import sha256_json
from multilabel_lab.pipeline import classify_experiment
from multilabel_lab.verifier import verify_report


def test_verifier_reports_first_score_decision_deviation(snapshot_factory):
    report = classify_experiment(snapshot_factory(sample_count=5))
    tampered = classify_experiment(snapshot_factory(sample_count=5))
    tampered["samples"][1]["stages"]["decision"]["decisions"][0] = not tampered[
        "samples"
    ][1]["stages"]["decision"]["decisions"][0]

    verification = verify_report(report, tampered)

    assert verification["status"] == "error"
    assert verification["result_code"] == "E_VERIFY_FAIL"
    assert verification["matched"] is False
    assert verification["first_deviation"]["kind"] == "rerun_mismatch"
    assert "samples[1]" in verification["first_deviation"]["location"]


def test_verifier_reports_stage_set_mismatch(snapshot_factory):
    report = classify_experiment(snapshot_factory(sample_count=4))
    report["stage_audit"]["feature"] = report["stage_audit"]["feature"][:-1]
    verification = verify_report(report)

    assert verification["result_code"] == "E_VERIFY_FAIL"
    assert verification["first_deviation"]["kind"] == "stage_sample_set_mismatch"
    assert verification["first_deviation"]["location"] == "$.stage_audit.feature"


def test_verifier_hashes_are_stable(snapshot_factory):
    snapshot = snapshot_factory(sample_count=4)
    first = classify_experiment(snapshot)
    second = classify_experiment(snapshot)
    verification = verify_report(first, second)
    assert verification["first_hash"] == sha256_json(first)
    assert verification["second_hash"] == sha256_json(second)
