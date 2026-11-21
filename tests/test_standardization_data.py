from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from linear_path_engine.config import load_config
from linear_path_engine.data import load_dataset
from linear_path_engine.errors import E_CONFIG, EngineError
from linear_path_engine.standardization import fit_standardization, stats_content_version

from .helpers import write_config, write_dataset


def test_dataset_order_and_encoding_are_deterministic(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=36, seed=22)
    config_path = write_config(tmp_path, data_path=data_path, run_id="data-order")
    config = load_config(config_path)

    dataset_a = load_dataset(config.data)
    lines = data_path.read_text(encoding="utf-8").splitlines()
    data_path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    dataset_b = load_dataset(config.data)

    assert dataset_a.fingerprint == dataset_b.fingerprint
    assert dataset_a.row_id_tokens == tuple(sorted(dataset_a.row_id_tokens))
    assert dataset_a.feature_names == (
        "num:x1",
        "num:x2",
        "num:x3",
        "cat:cat0=alpha",
        "cat:cat0=beta",
        "cat:cat0=gamma",
    )


@pytest.mark.parametrize("method", ["zscore", "minmax"])
def test_standardization_is_reproducible(tmp_path: Path, method: str) -> None:
    data_path = write_dataset(tmp_path, n_samples=40, seed=31)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id=f"standardize-{method}",
        standardization=method,
    )
    config = load_config(config_path)
    dataset = load_dataset(config.data)

    first = fit_standardization(dataset, config)
    second = fit_standardization(dataset, config)

    assert np.array_equal(first.matrix, second.matrix)
    assert first.stats == second.stats
    assert first.stats_version == second.stats_version
    assert stats_content_version(first.stats) == first.stats_version
    assert first.matrix.shape == (dataset.n_samples, dataset.n_features)
    numeric = first.matrix[:, :3]
    if method == "zscore":
        assert np.allclose(np.mean(numeric, axis=0), 0.0, atol=1e-12)
        assert np.allclose(np.std(numeric, axis=0), 1.0, atol=1e-12)
    else:
        assert np.allclose(np.min(numeric, axis=0), 0.0)
        assert np.allclose(np.max(numeric, axis=0), 1.0)


def test_config_rejects_descending_strengths(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=20, seed=5)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="bad-strengths",
        strengths=(0.2, 0.05, 0.01),
    )
    with pytest.raises(EngineError) as raised:
        load_config(config_path)
    assert raised.value.code == E_CONFIG


def test_duplicate_row_ids_are_rejected(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=20, seed=8)
    rows = [json.loads(line) for line in data_path.read_text(encoding="utf-8").splitlines()]
    rows[1]["row_id"] = rows[0]["row_id"]
    data_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    config_path = write_config(tmp_path, data_path=data_path, run_id="duplicate-ids")
    config = load_config(config_path)
    with pytest.raises(EngineError) as raised:
        load_dataset(config.data)
    assert raised.value.code == "E_DATA"

