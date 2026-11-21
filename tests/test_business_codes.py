from __future__ import annotations

import copy
from pathlib import Path

import pytest

from linear_path_engine.config import load_config
from linear_path_engine.data import load_dataset
from linear_path_engine.engine import TrainingEngine
from linear_path_engine.errors import (
    E_MONOTONICITY,
    E_OBJECTIVE_MISMATCH,
    E_RESOURCE_LIMIT,
    E_VERSION_MISMATCH,
    EngineError,
)
from linear_path_engine.validation import validate_result_document

from .helpers import write_config, write_dataset


def test_saved_result_can_be_revalidated_and_tampering_is_attributed(
    tmp_path: Path,
) -> None:
    data_path = write_dataset(tmp_path, n_samples=70, seed=101)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="codes-validation",
        strengths=(0.01, 0.05, 0.2),
    )
    config = load_config(config_path)
    dataset = load_dataset(config.data)
    result = TrainingEngine(config_path).run()
    report = validate_result_document(result, config=config, dataset=dataset)
    assert report["checked"] is True

    monotonic = copy.deepcopy(result)
    strongest = max(monotonic["path"], key=lambda item: item["strength"])
    strongest["coefficients"] = [value * 20.0 for value in strongest["coefficients"]]
    with pytest.raises(EngineError) as raised:
        validate_result_document(monotonic, config=config, dataset=dataset)
    assert raised.value.code == E_MONOTONICITY
    assert raised.value.strength == 0.2
    assert raised.value.strength_index == 2

    objective = copy.deepcopy(result)
    objective["path"][0]["objective"] += 1.0
    with pytest.raises(EngineError) as raised:
        validate_result_document(objective, config=config, dataset=dataset)
    assert raised.value.code == E_OBJECTIVE_MISMATCH
    assert raised.value.strength == 0.2
    assert raised.value.strength_index == 2

    versioned = copy.deepcopy(result)
    versioned["path"][0]["stats_version"] = "tampered"
    with pytest.raises(EngineError) as raised:
        validate_result_document(versioned, config=config, dataset=dataset)
    assert raised.value.code == E_VERSION_MISMATCH
    assert raised.value.strength == 0.2
    assert raised.value.strength_index == 2


def test_resource_limits_fail_with_business_code(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=60, seed=102)
    cases = [
        {"max_path_size": 1},
        {"max_iterations_per_strength": 3},
        {"comparison_budget": 100},
        {"memory_budget_bytes": 1000},
    ]
    for index, overrides in enumerate(cases):
        config_path = write_config(
            tmp_path,
            data_path=data_path,
            run_id=f"codes-resource-{index}",
            strengths=(0.01, 0.05, 0.2),
            **overrides,
        )
        with pytest.raises(EngineError) as raised:
            TrainingEngine(config_path).run()
        assert raised.value.code == E_RESOURCE_LIMIT


def test_degradation_is_declared_with_cost_evidence(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=90, seed=103)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="codes-degradation",
        workers=4,
        block_size=16,
        min_block_size=4,
        memory_budget_bytes=13_000,
        strengths=(0.01, 0.05, 0.2),
    )
    result = TrainingEngine(config_path).run()
    assert result["chunk_plan"]["degradation_categories"] == [
        "workers_reduced",
        "small_block",
    ]
    assert result["chunk_plan"]["coverage"]["complete"] is True
    assert (
        result["chunk_plan"]["estimated_resident_bytes"]
        <= result["chunk_plan"]["memory_budget_bytes"]
    )
    basis_factors = [item["factor"] for item in result["chunk_plan"]["decision_basis"]]
    assert "memory_guard_worker_reduction" in basis_factors
    assert "memory_guard_small_block" in basis_factors

