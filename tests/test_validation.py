from __future__ import annotations

import copy
from pathlib import Path

import pytest

from ctr_pipeline import BudgetConfig, TrainingConfig, run_pipeline
from ctr_pipeline.errors import E_INCONSISTENT, PipelineError
from ctr_pipeline.validation import validate_result


def _result(generated_dataset: Path, *, memory_budget: int = 32 * 1024 * 1024) -> dict:
    return run_pipeline(
        generated_dataset / "exposures.jsonl",
        generated_dataset / "features.jsonl",
        feature_dimension=128,
        training_config=TrainingConfig(
            chunk_size=8,
            batch_sample_size=24,
            workers=2,
            max_rounds=8,
            tolerance=1e-9,
        ),
        budget_config=BudgetConfig(
            total_mac_budget=50_000_000,
            memory_budget_bytes=memory_budget,
        ),
    )


def test_tampered_auc_returns_first_deviation(generated_dataset: Path) -> None:
    result = _result(generated_dataset)
    tampered = copy.deepcopy(result)
    tampered["chunked"]["metrics"]["auc"] += 0.01
    with pytest.raises(PipelineError) as caught:
        validate_result(tampered)
    error = caught.value
    assert error.code == E_INCONSISTENT
    assert error.details["category"] == "auc_mismatch"
    assert error.details["first_deviation"]["stage"] == "metric_validation"
    assert error.details["first_deviation"]["position"]["field"] == "chunked.metrics.auc"


def test_tampered_chunk_gradient_returns_parameter_position(
    generated_dataset: Path,
) -> None:
    result = _result(generated_dataset)
    tampered = copy.deepcopy(result)
    tampered["chunked"]["rounds"][0]["chunk_results"][0]["gradient"][3] += 1.0
    with pytest.raises(PipelineError) as caught:
        validate_result(tampered)
    position = caught.value.details["first_deviation"]["position"]
    assert caught.value.details["category"] == "gradient_reduction_mismatch"
    assert position["round"] == 1
    assert position["parameter_index"] == 3


def test_rejected_sample_cannot_appear_in_summary(generated_dataset: Path) -> None:
    result = _result(generated_dataset, memory_budget=80_000)
    rejected = result["stage_ids"]["rejected"][0]
    tampered = copy.deepcopy(result)
    tampered["summary"]["trained_sample_ids"] = [
        rejected,
        *tampered["summary"]["trained_sample_ids"],
    ]
    with pytest.raises(PipelineError) as caught:
        validate_result(tampered)
    assert caught.value.details["category"] == "sample_id_set"
    assert caught.value.details["first_deviation"]["position"]["first_sample_id"] == rejected


def test_degraded_result_is_valid(generated_dataset: Path) -> None:
    result = _result(generated_dataset, memory_budget=80_000)
    assert result["summary"]["degraded"] is True
    report = validate_result(result)
    assert report["status"] == "ok"
    assert report["checks"]["degraded_mode_supported"] is True


def test_budget_rejected_result_cannot_leak_sample_into_summary(
    generated_dataset: Path,
) -> None:
    result = run_pipeline(
        generated_dataset / "exposures.jsonl",
        generated_dataset / "features.jsonl",
        feature_dimension=128,
        training_config=TrainingConfig(
            chunk_size=8,
            batch_sample_size=24,
            workers=4,
            max_rounds=8,
            tolerance=1e-9,
        ),
        budget_config=BudgetConfig(
            total_mac_budget=50_000_000,
            memory_budget_bytes=10_000,
        ),
    )
    assert result["status"] == "budget_rejected"
    tampered = copy.deepcopy(result)
    tampered["summary"]["trained_sample_ids"] = [tampered["stage_ids"]["rejected"][0]]
    with pytest.raises(PipelineError) as caught:
        validate_result(tampered)
    assert caught.value.details["category"] == "rejected_sample_present"
    assert (
        caught.value.details["first_deviation"]["position"]["first_sample_id"]
        == result["stage_ids"]["rejected"][0]
    )
