from __future__ import annotations

import copy

from summarize_pipeline.errors import ErrorCode
from summarize_pipeline.utils import sha256_object
from summarize_pipeline.validator import (
    validate_report,
    verify_reproducibility,
)


def _refresh_hash(report: dict) -> None:
    payload = {key: value for key, value in report.items() if key != "run_hash"}
    report["run_hash"] = sha256_object(payload)


def test_valid_report_and_two_run_reproducibility(documents, config, report) -> None:
    validation = validate_report(report, config)
    assert validation["ok"] is True
    assert validation["issue_count"] == 0
    reproducibility = verify_reproducibility(documents, config)
    assert reproducibility["ok"] is True
    assert reproducibility["reproducibility"]["hash_match"] is True
    assert (
        reproducibility["reproducibility"]["first_run_hash"]
        == reproducibility["reproducibility"]["second_run_hash"]
    )


def test_quality_tamper_returns_inconsistent_with_category(report, config) -> None:
    tampered = copy.deepcopy(report)
    doc_id = next(iter(tampered["precisions"]["float64"]["documents"]))
    doc = tampered["precisions"]["float64"]["documents"][doc_id]
    doc["quality"]["quality_score"] = 0.123
    _refresh_hash(tampered)
    validation = validate_report(tampered, config)
    assert validation["ok"] is False
    assert validation["error"]["code"] == ErrorCode.INCONSISTENT
    assert any(
        issue["category"] == "quality_score_recompute"
        and issue["doc_id"] == doc_id
        for issue in validation["issues"]
    )


def test_fixed_delta_tamper_is_detected(report, config) -> None:
    tampered = copy.deepcopy(report)
    sample = next(
        row
        for row in tampered["error_spectra"]["fixed"]["per_sentence"]
        if "fixed_raw" in row
    )
    doc_id = sample["doc_id"]
    sample["quantization_delta_from_reference"] += 0.25
    _refresh_hash(tampered)
    validation = validate_report(tampered, config)
    assert any(
        issue["category"] == "fixed_deviation_recompute"
        and issue["doc_id"] == doc_id
        for issue in validation["issues"]
    )


def test_selection_and_interval_tamper_are_detected(report, config) -> None:
    tampered = copy.deepcopy(report)
    doc_id = next(iter(tampered["precisions"]["float32"]["documents"]))
    doc = tampered["precisions"]["float32"]["documents"][doc_id]
    doc["selection"]["selected_in_score_order"] = list(
        reversed(doc["selection"]["selected_in_score_order"])
    )
    spectrum = tampered["error_spectra"]["float32"]["stages"]["smoothing"]
    spectrum["error_interval"]["low_abs"] = 99.0
    spectrum["error_interval"]["high_abs"] = 1.0
    _refresh_hash(tampered)
    validation = validate_report(tampered, config)
    categories = {issue["category"] for issue in validation["issues"]}
    assert "selection_recompute" in categories
    assert "error_interval_order" in categories

