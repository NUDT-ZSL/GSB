from __future__ import annotations

from pathlib import Path

import pytest

from ctr_pipeline import BudgetConfig, TrainingConfig, run_pipeline
from ctr_pipeline.errors import E_COMPUTE_BUDGET, E_MEMORY_LIMIT, PipelineError


def _base_config() -> TrainingConfig:
    return TrainingConfig(
        chunk_size=8,
        batch_sample_size=24,
        workers=4,
        max_rounds=8,
        tolerance=1e-9,
    )


def test_compute_budget_degrades_to_stable_prefix(generated_dataset: Path) -> None:
    result = run_pipeline(
        generated_dataset / "exposures.jsonl",
        generated_dataset / "features.jsonl",
        feature_dimension=256,
        training_config=_base_config(),
        budget_config=BudgetConfig(
            total_mac_budget=30_000,
            memory_budget_bytes=32 * 1024 * 1024,
        ),
    )
    decision = result["budget"]["decision"]
    assert decision["admitted_sample_count"] == 24
    assert decision["rejected_sample_count"] == 24
    assert decision["degrade_categories"] == [E_COMPUTE_BUDGET]
    assert decision["effective_workers"] == 1
    assert result["summary"]["degraded"] is True
    assert result["summary"]["budget_error_code"] == E_COMPUTE_BUDGET
    assert result["validation"]["status"] == "ok"
    report = result["budget"]["report"]
    assert report["priority_rules"]
    assert report["batch_routes"]
    assert report["feature_projection"]["applied"] is False
    assert all(route["path"] == "restricted" for route in report["batch_routes"])
    admitted = result["stage_ids"]["admitted"]
    rejected = result["stage_ids"]["rejected"]
    assert admitted + rejected == result["stage_ids"]["ingested"]
    assert set(admitted).isdisjoint(rejected)


def test_memory_budget_degrades_with_smaller_chunks(generated_dataset: Path) -> None:
    result = run_pipeline(
        generated_dataset / "exposures.jsonl",
        generated_dataset / "features.jsonl",
        feature_dimension=256,
        training_config=TrainingConfig(
            chunk_size=8,
            batch_sample_size=24,
            workers=3,
            max_rounds=8,
            tolerance=1e-9,
        ),
        budget_config=BudgetConfig(
            total_mac_budget=50_000_000,
            memory_budget_bytes=75_000,
        ),
    )
    decision = result["budget"]["decision"]
    assert decision["degrade_categories"] == [E_MEMORY_LIMIT]
    assert decision["admitted_sample_count"] == 6
    assert decision["rejected_sample_count"] == 42
    assert decision["effective_chunk_size"] == 2
    assert result["summary"]["budget_error_code"] == E_MEMORY_LIMIT
    assert result["validation"]["status"] == "ok"


def test_strict_budget_raises_selected_error(generated_dataset: Path) -> None:
    with pytest.raises(PipelineError) as caught:
        run_pipeline(
            generated_dataset / "exposures.jsonl",
            generated_dataset / "features.jsonl",
            feature_dimension=256,
            training_config=_base_config(),
            budget_config=BudgetConfig(
                total_mac_budget=30_000,
                memory_budget_bytes=32 * 1024 * 1024,
                allow_degraded=False,
            ),
        )
    assert caught.value.code == E_COMPUTE_BUDGET
    assert caught.value.stage == "budget_admission"
    assert caught.value.details["rejected_sample_count"] == 24
