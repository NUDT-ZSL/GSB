from __future__ import annotations

from pathlib import Path

from ctr_pipeline import BudgetConfig, TrainingConfig, run_pipeline
from ctr_pipeline.canonical import checksum


def _run(
    generated_dataset: Path,
    *,
    workers: int,
    fault_chunk_ids: tuple[int, ...] = (),
    max_rounds: int = 8,
) -> dict:
    return run_pipeline(
        generated_dataset / "exposures.jsonl",
        generated_dataset / "features.jsonl",
        feature_dimension=128,
        training_config=TrainingConfig(
            chunk_size=8,
            batch_sample_size=24,
            workers=workers,
            max_rounds=max_rounds,
            tolerance=1e-9,
            fault_chunk_ids=fault_chunk_ids,
        ),
        budget_config=BudgetConfig(
            total_mac_budget=50_000_000,
            memory_budget_bytes=32 * 1024 * 1024,
        ),
    )


def test_parallel_worker_count_does_not_change_training_bytes(
    generated_dataset: Path,
) -> None:
    single = _run(generated_dataset, workers=1)
    parallel = _run(generated_dataset, workers=3)
    assert single["status"] == parallel["status"] == "ok"
    assert single["determinism_fingerprint"] == parallel["determinism_fingerprint"]
    assert checksum(single["chunked"]["rounds"]) == checksum(
        parallel["chunked"]["rounds"]
    )
    assert single["chunked"]["parameters"] == parallel["chunked"]["parameters"]
    assert [chunk["chunk_id"] for chunk in single["chunked"]["chunks"]] == [
        chunk["chunk_id"] for chunk in parallel["chunked"]["chunks"]
    ]
    assert single["validation"]["status"] == "ok"


def test_chunked_and_full_batch_are_cross_checked(
    generated_dataset: Path,
) -> None:
    result = _run(generated_dataset, workers=2)
    comparison = result["comparison"]
    assert comparison["max_abs_parameter_deviation"] < 1e-12
    assert comparison["converged_round_equal"] is True
    assert len(comparison["final_parameter_abs_deviation_spectrum"]) == 129
    assert len(comparison["round_deltas"]) == len(result["chunked"]["rounds"])
    assert all(
        row["max_abs_gradient_delta"] < 1e-12 for row in comparison["round_deltas"]
    )


def test_worker_failure_preserves_successful_chunks_and_attributes_batch(
    generated_dataset: Path,
) -> None:
    result = _run(
        generated_dataset,
        workers=2,
        fault_chunk_ids=(1,),
        max_rounds=3,
    )
    assert result["status"] == "partial"
    assert result["chunked"]["complete"] is False
    assert len(result["chunked"]["partial_chunks"]) == 6
    assert sum(1 for row in result["chunked"]["partial_chunks"] if row["ok"]) == 5
    error = result["chunked"]["errors"][0]
    assert error["code"] == "E_WORKER"
    assert error["stage"] == "chunk_gradient"
    assert error["details"]["batch_index"] == 0
    assert error["details"]["chunk_index"] == 1
    assert error["details"]["first_sample_id"] == "s000009"
