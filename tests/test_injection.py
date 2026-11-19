from __future__ import annotations

import pytest

from multilabel_lab.errors import E_INCONSISTENT, E_UNATTRIBUTED, LabError
from multilabel_lab.decision import validate_decision_alignment
from multilabel_lab.pipeline import classify_experiment
from multilabel_lab.verifier import verify_report


def multi_injections():
    return [
        {
            "stage": "preprocess",
            "type": "pixel_out_of_range",
            "sample_id": "sample-0000",
            "params": {"row": 0, "col": 0, "channel": 0, "value": 300},
        },
        {
            "stage": "feature",
            "type": "projection_shape_mismatch",
            "sample_id": "sample-0001",
            "params": {"shape": [3, 3]},
        },
        {
            "stage": "score",
            "type": "label_index_oob",
            "sample_id": "sample-0002",
            "label": "ghost",
            "params": {"label_index": 99, "label": "ghost"},
        },
        {
            "stage": "decision",
            "type": "nonpositive_threshold",
            "sample_id": "sample-0003",
            "label": "cat",
            "params": {"label": "cat", "threshold": 0.0},
        },
    ]


def test_all_four_injection_stages_are_attributed_and_degraded(snapshot_factory):
    snapshot = snapshot_factory(sample_count=8)
    report = classify_experiment(snapshot, multi_injections())

    assert report["status"] == "degraded"
    assert report["result_code"] == "OK_DEGRADED"
    assert len(report["injection_records"]) == 4
    assert [record["stage"] for record in report["injection_records"]] == [
        "preprocess",
        "feature",
        "score",
        "decision",
    ]
    assert all(record["sample_id"] for record in report["injection_records"])
    assert all(record["params"] for record in report["injection_records"])

    expected = {
        ("preprocess", "DEGRADE_CLAMP_PIXELS", "E_PIXEL_RANGE"),
        ("feature", "DEGRADE_RAW_BLOCK_STATS", "E_PROJECTION_SHAPE"),
        ("score", "DEGRADE_BIAS_ONLY_SCORE", "E_LABEL_INDEX"),
        ("decision", "DEGRADE_DEFAULT_THRESHOLD", "E_THRESHOLD"),
    }
    actual = {
        (record["stage"], record["category"], record["triggered_by"])
        for record in report["degradations"]
    }
    assert actual == expected
    assert report["metrics_path"]["mode"] == "degraded"
    assert report["metrics_path"]["source"] == "degraded_predictions"
    assert set(report["business_codes"]) == {
        "E_PIXEL_RANGE",
        "E_PROJECTION_SHAPE",
        "E_LABEL_INDEX",
        "E_THRESHOLD",
    }
    assert report["samples"][0]["stages"]["preprocess"]["path"] == "clamped"
    assert report["samples"][1]["stages"]["feature"]["path"] == "raw_block_stats"
    assert report["samples"][2]["stages"]["score"]["path"] == "bias_only"
    assert report["samples"][3]["stages"]["decision"]["path"] == "fallback_thresholds"
    assert verify_report(report, classify_experiment(snapshot, multi_injections()))[
        "result_code"
    ] == "OK"


def test_no_injection_means_no_injection_or_degradation_records(snapshot_factory):
    report = classify_experiment(snapshot_factory(sample_count=5))
    assert report["injection_records"] == []
    assert report["degradations"] == []


def test_unattributed_exception_is_reported_not_swallowed(snapshot_factory, monkeypatch):
    import multilabel_lab.pipeline as pipeline

    snapshot = snapshot_factory(sample_count=3)

    def explode(_image, _config):
        raise RuntimeError("synthetic unattributed failure")

    monkeypatch.setattr(pipeline, "block_statistics", explode)
    report = pipeline.classify_experiment(snapshot)

    assert report["status"] == "error"
    assert report["result_code"] == E_UNATTRIBUTED
    assert len(report["unattributed"]) == 3
    assert all(
        record["category"] == "DEGRADE_UNATTRIBUTED_FALLBACK"
        for record in report["degradations"]
    )
    assert report["metrics_path"]["mode"] == "degraded"


def test_inconsistent_decision_alignment_names_sample_and_label():
    with pytest.raises(LabError) as caught:
        validate_decision_alignment(
            sample_id="sample-0009",
            labels=["cat", "dog"],
            scores=[0.9, 0.1],
            thresholds=[0.5, 0.5],
            decisions=[True, False],
            predicted_labels=["dog"],
        )
    assert caught.value.code == E_INCONSISTENT
    assert caught.value.sample_id == "sample-0009"
    assert caught.value.label == "cat"
