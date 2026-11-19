from __future__ import annotations

import pytest

from multilabel_lab.errors import E_METRIC_UNDEFINED, LabError
from multilabel_lab.metrics import compute_metrics, recompute_metrics_from_details


LABELS = ["A", "B", "C"]


def test_manual_multilabel_metrics(snapshot_factory):
    records = [
        {
            "sample_id": "s1",
            "truth_labels": ["A", "B"],
            "uncertain_labels": [],
            "predicted_labels": ["A"],
        },
        {
            "sample_id": "s2",
            "truth_labels": ["C"],
            "uncertain_labels": [],
            "predicted_labels": ["B", "C"],
        },
    ]
    result = compute_metrics(
        records,
        LABELS,
        uncertain_strategy="ignore",
        zero_division="zero",
    )

    assert result["micro"]["tp"] == 2
    assert result["micro"]["fp"] == 1
    assert result["micro"]["fn"] == 1
    assert result["micro"]["precision"] == pytest.approx(2 / 3)
    assert result["micro"]["recall"] == pytest.approx(2 / 3)
    assert result["micro"]["f1"] == pytest.approx(2 / 3)
    assert result["macro_f1"] == pytest.approx((1.0 + 0.0 + 1.0) / 3)
    assert result["hamming_loss"] == pytest.approx(2 / 6)
    assert len(result["details"]) == 6
    by_label = {entry["label"]: entry for entry in result["per_label"]}
    assert by_label["A"]["precision"] == pytest.approx(1.0)
    assert by_label["A"]["recall"] == pytest.approx(1.0)
    assert by_label["B"]["f1"] == 0.0
    assert by_label["C"]["f1"] == 1.0

    recomputed = recompute_metrics_from_details(
        result["details"], LABELS, zero_division="zero"
    )
    for key in (
        "micro",
        "macro_f1",
        "macro_label_count",
        "per_label",
        "hamming_loss",
        "included_cells",
    ):
        assert recomputed[key] == result[key]


def test_zero_support_requires_declared_policy():
    records = [
        {
            "sample_id": "s1",
            "truth_labels": ["A"],
            "uncertain_labels": [],
            "predicted_labels": ["A"],
        }
    ]
    labels = ["A", "B"]

    zero = compute_metrics(
        records, labels, uncertain_strategy="ignore", zero_division="zero"
    )
    by_label = {entry["label"]: entry for entry in zero["per_label"]}
    assert by_label["B"]["precision"] == 0.0
    assert by_label["B"]["recall"] == 0.0
    assert by_label["B"]["f1"] == 0.0

    skipped = compute_metrics(
        records, labels, uncertain_strategy="ignore", zero_division="skip"
    )
    skipped_by_label = {entry["label"]: entry for entry in skipped["per_label"]}
    assert skipped_by_label["B"]["f1"] is None
    assert skipped["macro_f1"] == 1.0

    with pytest.raises(LabError) as caught:
        compute_metrics(
            records, labels, uncertain_strategy="ignore", zero_division="error"
        )
    assert caught.value.code == E_METRIC_UNDEFINED
    assert caught.value.label == "B"

    with pytest.raises(LabError) as missing:
        compute_metrics(
            records, labels, uncertain_strategy="ignore", zero_division=None
        )
    assert missing.value.code == E_METRIC_UNDEFINED
    assert missing.value.label == "A"


def test_uncertain_label_strategies():
    records = [
        {
            "sample_id": "s1",
            "truth_labels": ["A"],
            "uncertain_labels": ["B"],
            "predicted_labels": ["B"],
        }
    ]
    ignored = compute_metrics(
        records,
        ["A", "B"],
        uncertain_strategy="ignore",
        zero_division="zero",
    )
    ignored_b = next(entry for entry in ignored["per_label"] if entry["label"] == "B")
    assert ignored["uncertainty"]["strategy"] == "ignore"
    assert ignored["uncertainty"]["excluded_cells"] == 1
    assert ignored_b["included_cells"] == 0

    positive = compute_metrics(
        records,
        ["A", "B"],
        uncertain_strategy="positive",
        zero_division="zero",
    )
    positive_b = next(entry for entry in positive["per_label"] if entry["label"] == "B")
    assert positive_b["tp"] == 1
    assert positive_b["f1"] == 1.0

    separate = compute_metrics(
        records,
        ["A", "B"],
        uncertain_strategy="separate",
        zero_division="zero",
    )
    separate_b = next(entry for entry in separate["per_label"] if entry["label"] == "B")
    assert separate_b["included_cells"] == 0
    assert separate["uncertainty"]["excluded_cells"] == 1
    assert separate["uncertainty"]["per_label"][1]["predicted_positive"] == 1
