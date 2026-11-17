from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from gbdt_workbench.config import ConfigValidationError, load_config
from gbdt_workbench.data import generate_dataset
from gbdt_workbench.errors import E_CONFIG_INVALID
from gbdt_workbench.pipeline import WorkbenchEngine

from .helpers import ROOT, write_config


def test_same_config_and_seed_generate_elementwise_identical_data(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    loaded = load_config(config_path)
    first = generate_dataset(loaded)
    second = generate_dataset(load_config(config_path))

    assert first.data_fingerprint == second.data_fingerprint
    assert np.array_equal(first.X, second.X)
    assert np.array_equal(first.y, second.y)
    assert np.all(np.isfinite(first.X))
    assert set(np.unique(first.y).tolist()).issubset({0, 1})
    assert first.missing_counts["customer_score"] > 0
    assert "plan" in first.category_maps


@pytest.mark.parametrize(
    ("config_relpath", "expected_field"),
    [
        ("configs/anomalies/bad_learning_rate.yaml", "config.experiment.learning_rate"),
        ("configs/anomalies/bad_types.yaml", "config.experiment.n_trees"),
        ("configs/anomalies/unknown_column_config.yaml", "data_spec.columns[1].source"),
        ("configs/anomalies/missing_target_config.yaml", "data_spec.target.name"),
    ],
)
def test_invalid_configuration_enters_failure_report(
    tmp_path: Path,
    config_relpath: str,
    expected_field: str,
) -> None:
    result = WorkbenchEngine(ROOT / config_relpath, tmp_path / "out").run()

    assert result.exit_code == 2
    assert result.status == "failed"
    assert result.diagnostic_report["failures"]
    assert all(
        item["code"] == E_CONFIG_INVALID
        for item in result.diagnostic_report["failures"]
    )
    assert any(
        item.get("field") == expected_field
        for item in result.diagnostic_report["failures"]
    )


@pytest.mark.parametrize(
    "experiment_override",
    [
        {"n_trees": 0},
        {"max_depth": 0},
        {"learning_rate": -0.01},
        {"n_trees": "not-an-int"},
    ],
)
def test_parameter_validation_codes(
    tmp_path: Path,
    experiment_override: dict[str, object],
) -> None:
    config_path = write_config(tmp_path, experiment=experiment_override)
    with pytest.raises(ConfigValidationError) as caught:
        load_config(config_path)
    assert {item.code for item in caught.value.failures} == {E_CONFIG_INVALID}


def test_unknown_column_and_target_missing_reasons(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError) as unknown:
        load_config(ROOT / "configs/anomalies/unknown_column_config.yaml")
    assert any(
        item.details.get("reason") == "unknown_column"
        for item in unknown.value.failures
    )

    with pytest.raises(ConfigValidationError) as missing:
        load_config(ROOT / "configs/anomalies/missing_target_config.yaml")
    assert any(
        item.details.get("reason") == "target_column_missing"
        for item in missing.value.failures
    )

