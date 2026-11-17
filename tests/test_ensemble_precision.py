from __future__ import annotations

import math
from pathlib import Path

from gbdt_workbench.pipeline import WorkbenchEngine

from .helpers import read_json, rewrite_config, write_config


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def test_ensemble_scores_and_contributions_have_stable_shapes(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        experiment={"n_trees": 6, "tolerance": 0.0},
    )
    result = WorkbenchEngine(config_path, tmp_path / "out").run()
    report = result.diagnostic_report

    assert result.exit_code == 0
    assert all(0.0 <= value <= 1.0 for value in report["ensemble"]["probabilities"])
    feature_count = len(report["data"]["feature_names"])
    assert len(report["contributions"]["tree_contributions"]) == report["data"]["rows"]
    assert len(report["contributions"]["tree_contributions"][0]) == 6
    assert len(report["contributions"]["tree_contributions"][0][0]) == feature_count
    assert len(report["contributions"]["feature_contributions"][0]) == feature_count
    assert report["error_attribution"]["first_exceeding_stage"] == "split_gain"
    assert (
        report["error_attribution"]["stage_summaries"]["split_gain"]["max_abs_diff"]
        > 0.0
    )
    assert report["error_attribution"]["reference_precision"] == "float64"


def test_threshold_sensitive_sample_is_attributed_to_dominant_feature(
    tmp_path: Path,
) -> None:
    config_path = write_config(
        tmp_path,
        experiment={"n_trees": 6, "tolerance": 0.0},
    )
    output = tmp_path / "out"
    first = WorkbenchEngine(config_path, output).run()
    first_report = first.diagnostic_report
    base_probabilities = first_report["ensemble"]["probabilities"]
    candidates: list[tuple[float, int, float, float]] = []
    for item in first_report["error_attribution"]["items"]["ensemble_sum"]:
        sample_index = int(item["sample_index"])
        base_probability = float(base_probabilities[sample_index])
        reference_probability = _sigmoid(float(item["reference"]))
        difference = abs(reference_probability - base_probability)
        if difference > 0.0:
            candidates.append(
                (difference, sample_index, base_probability, reference_probability)
            )
    assert candidates
    _, target_index, base_probability, reference_probability = max(candidates)
    threshold = (base_probability + reference_probability) / 2.0
    assert 0.0 < threshold < 1.0

    rewrite_config(config_path, diagnostics={"decision_threshold": threshold})
    second = WorkbenchEngine(config_path, output).run()
    report = second.diagnostic_report

    assert second.exit_code == 0
    sensitive = {
        int(item["sample_index"]): item
        for item in report["threshold_sensitive_samples"]
    }
    assert target_index in sensitive
    sample = sensitive[target_index]
    assert sample["dominant_feature"] in report["data"]["feature_names"]
    assert sample["dominant_contribution"] != 0.0
    audit = second.run_report["operational_audit"]
    assert sorted(audit["reused"]["tree_indices"]) == list(range(6))
    assert audit["computed"]["tree_indices"] == []


def test_float64_uses_decimal_reference_precision(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        experiment={"precision": "float64", "tolerance": 1.0e-12},
    )
    result = WorkbenchEngine(config_path, tmp_path / "out").run()
    assert result.exit_code == 0
    assert (
        result.diagnostic_report["error_attribution"]["base_precision"] == "float64"
    )
    assert (
        result.diagnostic_report["error_attribution"]["reference_precision"]
        == "decimal"
    )
    persisted = read_json(result.diagnostic_path)
    assert persisted["equivalence_digest"] == result.diagnostic_report["equivalence_digest"]

