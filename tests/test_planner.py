from __future__ import annotations

from pathlib import Path

import pytest

from linear_path_engine.config import load_config
from linear_path_engine.data import load_dataset
from linear_path_engine.errors import E_RESOURCE_LIMIT, EngineError
from linear_path_engine.planner import plan_chunks
from linear_path_engine.util import canonical_json_bytes

from .helpers import write_config, write_dataset


def _inputs(tmp_path: Path, **config_overrides):
    data_path = write_dataset(tmp_path, n_samples=90, seed=41)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id=config_overrides.pop("run_id", "planner"),
        **config_overrides,
    )
    config = load_config(config_path)
    dataset = load_dataset(config.data)
    return config, dataset


def test_planner_is_byte_deterministic_and_covers_every_row(tmp_path: Path) -> None:
    config, dataset = _inputs(tmp_path, block_size=16, workers=3)
    iterations = (20, 20, 20)
    first = plan_chunks(
        config,
        n_samples=dataset.n_samples,
        n_features=dataset.n_features,
        iteration_counts_ascending=iterations,
    )
    second = plan_chunks(
        config,
        n_samples=dataset.n_samples,
        n_features=dataset.n_features,
        iteration_counts_ascending=iterations,
    )
    assert canonical_json_bytes(first.to_dict()) == canonical_json_bytes(second.to_dict())
    boundaries = first.boundaries()
    assert boundaries[0][0] == 0
    assert boundaries[-1][1] == dataset.n_samples
    assert all(
        boundaries[index - 1][1] == boundaries[index][0]
        for index in range(1, len(boundaries))
    )
    assert sum(end - start for start, end in boundaries) == dataset.n_samples
    assert first.reduction_order == "block_index_ascending"
    assert first.dispatch_order == "block_index_ascending"
    assert first.to_dict()["coverage"]["complete"] is True
    assert first.decision_checksum


def test_memory_guard_uses_declared_degradations(tmp_path: Path) -> None:
    config, dataset = _inputs(
        tmp_path,
        run_id="planner-degrade",
        workers=4,
        block_size=16,
        min_block_size=4,
        memory_budget_bytes=12_000,
    )
    plan = plan_chunks(
        config,
        n_samples=dataset.n_samples,
        n_features=dataset.n_features,
        iteration_counts_ascending=(10, 10, 10),
    )
    assert plan.workers == 1
    assert plan.block_size == 4
    assert plan.degradation_categories == ("workers_reduced", "small_block")
    assert plan.small_block_downgrade is True
    assert plan.worker_downgrade is True
    assert plan.estimated_resident_bytes <= config.execution.memory_budget_bytes
    assert sum(end - start for start, end in plan.boundaries()) == dataset.n_samples


def test_comparison_budget_fails_before_execution(tmp_path: Path) -> None:
    config, dataset = _inputs(
        tmp_path,
        run_id="planner-comparison",
        comparison_budget=100,
    )
    with pytest.raises(EngineError) as raised:
        plan_chunks(
            config,
            n_samples=dataset.n_samples,
            n_features=dataset.n_features,
            iteration_counts_ascending=(100, 100, 100),
        )
    assert raised.value.code == E_RESOURCE_LIMIT
    assert "estimated_comparisons" in raised.value.details


def test_planner_uses_remaining_not_total_budget(tmp_path: Path) -> None:
    config, dataset = _inputs(
        tmp_path,
        run_id="planner-remaining",
        comparison_budget=1_000,
    )
    with pytest.raises(EngineError) as raised:
        plan_chunks(
            config,
            n_samples=dataset.n_samples,
            n_features=dataset.n_features,
            iteration_counts_ascending=(5, 5, 5),
            comparisons_already_used=900,
        )
    assert raised.value.code == E_RESOURCE_LIMIT
    assert raised.value.details["remaining_comparison_budget"] == 100

