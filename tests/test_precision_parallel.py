from __future__ import annotations

from pathlib import Path
import json

import numpy as np
import pytest

from linear_path_engine.engine import TrainingEngine
from linear_path_engine.errors import E_WORKER, EngineError

from .helpers import write_config, write_dataset


@pytest.mark.parametrize("mode", ["float64", "float32", "fixed_point"])
def test_all_precision_modes_share_path_semantics(tmp_path: Path, mode: str) -> None:
    data_path = write_dataset(tmp_path, n_samples=70, seed=71)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id=f"precision-{mode}",
        mode=mode,
        workers=1,
        strengths=(0.01, 0.05, 0.2),
    )
    result = TrainingEngine(config_path).run()

    assert result["precision"]["mode"] == mode
    assert [entry["strength"] for entry in result["path"]] == [0.2, 0.05, 0.01]
    assert all(entry["converged"] for entry in result["path"])
    assert all(entry["stats_version"] == result["standardization"]["stats_version"] for entry in result["path"])
    assert result["validation"]["checked"] is True
    norms = [
        entry["coefficient_norm_l2"]
        for entry in sorted(result["path"], key=lambda item: item["strength"])
    ]
    assert norms == sorted(norms, reverse=True)
    if mode == "fixed_point":
        scale = result["precision"]["fixed_scale"]
        for entry in result["path"]:
            quantized = np.asarray(entry["coefficients"]) * scale
            assert np.allclose(quantized, np.rint(quantized), atol=1e-9)
            assert abs(entry["intercept"] * scale - round(entry["intercept"] * scale)) < 1e-9


def test_precision_loss_is_bounded(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=70, seed=72)
    results = {}
    for mode in ("float64", "float32", "fixed_point"):
        config_path = write_config(
            tmp_path,
            data_path=data_path,
            run_id=f"precision-compare-{mode}",
            mode=mode,
            workers=1,
            strengths=(0.02, 0.1),
            max_iterations=700,
        )
        results[mode] = TrainingEngine(config_path).run()
    reference = np.asarray(results["float64"]["path"][0]["coefficients"])
    assert np.max(np.abs(np.asarray(results["float32"]["path"][0]["coefficients"]) - reference)) < 2e-3
    assert np.max(np.abs(np.asarray(results["fixed_point"]["path"][0]["coefficients"]) - reference)) < 2e-2


def test_parallel_and_serial_gradient_reduction_are_identical(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=72, seed=73)
    serial_config = write_config(
        tmp_path,
        data_path=data_path,
        run_id="parallel-serial",
        workers=1,
        block_size=16,
        strengths=(0.01, 0.05, 0.2),
    )
    parallel_config = write_config(
        tmp_path,
        data_path=data_path,
        run_id="parallel-workers",
        workers=3,
        block_size=16,
        strengths=(0.01, 0.05, 0.2),
    )
    serial = TrainingEngine(serial_config).run()
    parallel = TrainingEngine(parallel_config).run()
    assert serial["path"] == parallel["path"]
    assert serial["summary"]["path_checksum"] == parallel["summary"]["path_checksum"]
    assert serial["resource_usage"]["comparisons_used"] == parallel["resource_usage"]["comparisons_used"]
    assert parallel["chunk_plan"]["workers"] == 3
    assert parallel["chunk_plan"]["dispatch_order"] == "block_index_ascending"
    assert parallel["chunk_plan"]["reduction_order"] == "block_index_ascending"


def test_worker_exception_is_attributed_to_strength_and_block(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=64, seed=74)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="worker-fault",
        workers=2,
        strengths=(0.01, 0.05, 0.2),
    )
    engine = TrainingEngine(config_path)
    with pytest.raises(EngineError) as raised:
        engine.run(fault_worker_at="2:1")
    error = raised.value
    assert error.code == E_WORKER
    assert error.stage == "PATH_SOLVE"
    assert error.strength_index == 2
    assert error.strength == 0.2
    assert error.block_index == 1
    assert error.details["worker_error"]["exception_type"] == "RuntimeError"
    current = engine.store.load_current()
    assert current is not None
    assert current.stage == "standardization"
    assert current.payload["path"] == []


def test_l2_regularization_path_is_supported(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=60, seed=75)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="l2-path",
        workers=1,
        strengths=(0.01, 0.1),
    )
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["model"]["regularization"] = {"type": "l2"}
    config_path.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    result = TrainingEngine(config_path).run()
    assert result["model"]["regularization"] == "l2"
    assert result["model"]["l1_ratio"] == 0.0
    assert len(result["path"]) == 2
    assert all(entry["converged"] for entry in result["path"])
    assert result["validation"]["checked"] is True


def test_precision_switch_changes_only_numeric_representation(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=60, seed=76)
    results = {}
    for mode in ("float64", "float32", "fixed_point"):
        config_path = write_config(
            tmp_path,
            data_path=data_path,
            run_id="precision-isolated",
            mode=mode,
            workers=2,
            strengths=(0.02, 0.1),
            block_size=16,
            max_iterations=600,
            tolerance=1.0e-6,
            objective_abs_tolerance=1.0e-4,
            objective_rel_tolerance=1.0e-4,
            monotonicity_abs_tolerance=1.0e-3,
            monotonicity_rel_tolerance=0.01,
            suffix=f"-{mode}",
        )
        results[mode] = TrainingEngine(config_path).run()

    reference = results["float64"]
    for mode in ("float32", "fixed_point"):
        candidate = results[mode]
        assert [item["strength"] for item in candidate["path"]] == [
            item["strength"] for item in reference["path"]
        ]
        assert candidate["chunk_plan"]["boundaries"] == reference["chunk_plan"]["boundaries"]
        assert candidate["model"] == reference["model"]
        assert candidate["standardization"]["stats"] == reference["standardization"]["stats"]
        assert candidate["validation"]["checked"] is True
        for expected in reference["path"]:
            actual = next(
                item for item in candidate["path"] if item["strength"] == expected["strength"]
            )
            difference = np.max(
                np.abs(
                    np.asarray(actual["coefficients"])
                    - np.asarray(expected["coefficients"])
                )
            )
            assert difference < (2e-2 if mode == "fixed_point" else 2e-3)


@pytest.mark.parametrize("rounding", ["nearest_even", "nearest_away", "truncate"])
def test_fixed_point_rounding_modes_execute_same_path_semantics(
    tmp_path: Path, rounding: str
) -> None:
    data_path = write_dataset(tmp_path, n_samples=40, seed=77)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id=f"rounding-{rounding}",
        mode="fixed_point",
        workers=1,
        strengths=(0.02, 0.1),
        fixed_rounding=rounding,
        max_iterations=300,
    )
    result = TrainingEngine(config_path).run()
    assert result["precision"]["fixed_rounding"] == rounding
    assert result["validation"]["checked"] is True
    scale = result["precision"]["fixed_scale"]
    for entry in result["path"]:
        quantized = np.asarray(entry["coefficients"]) * scale
        assert np.allclose(quantized, np.rint(quantized), atol=1e-9)

