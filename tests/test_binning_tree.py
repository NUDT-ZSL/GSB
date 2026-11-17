from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

from gbdt_workbench.binning import build_binning
from gbdt_workbench.config import load_config
from gbdt_workbench.data import Dataset, generate_dataset
from gbdt_workbench.precision import PrecisionContext
from gbdt_workbench.tree import train_tree

from .helpers import write_config


def test_binning_is_repeatable_and_uses_integer_boundaries(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    dataset = generate_dataset(load_config(config_path))
    first = build_binning(dataset, 8)
    second = build_binning(dataset, 8)

    assert first["fingerprint"] == second["fingerprint"]
    assert np.array_equal(first["sample_bins"], second["sample_bins"])
    numeric = next(item for item in first["features"] if item["name"] == "monthly_usage")
    assert len(numeric["boundaries"]) == 9
    assert all(isinstance(value, int) for value in numeric["boundaries"])
    assert numeric["boundaries"][0] == 0
    assert numeric["boundaries"][-1] == dataset.rows
    assert sum(numeric["bin_counts"]) == dataset.rows


def _tiny_dataset() -> Dataset:
    rows = 8
    X = np.column_stack(
        [
            np.arange(rows, dtype=np.float64),
            np.arange(rows, dtype=np.float64),
        ]
    )
    return Dataset(
        feature_names=("first", "second"),
        feature_kinds=("numeric", "numeric"),
        target_name="target",
        X=X,
        y=np.asarray([0, 0, 0, 0, 1, 1, 1, 1], dtype=np.uint8),
        category_maps={},
        fill_values={},
        missing_counts={},
        data_fingerprint="tiny-dataset",
        seed=7,
    )


def test_equal_frequency_ties_follow_original_row_order() -> None:
    dataset = Dataset(
        feature_names=("constant",),
        feature_kinds=("numeric",),
        target_name="target",
        X=np.asarray([[1.0], [1.0], [1.0], [1.0]], dtype=np.float64),
        y=np.asarray([0, 1, 0, 1], dtype=np.uint8),
        category_maps={},
        fill_values={},
        missing_counts={},
        data_fingerprint="tie-dataset",
        seed=3,
    )
    result = build_binning(dataset, 2)
    assert result["features"][0]["boundaries"] == [0, 2, 4]
    assert result["sample_bins"][:, 0].tolist() == [0, 0, 1, 1]


def test_split_gain_tie_breaks_on_lower_feature_index(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    loaded = load_config(config_path)
    config = replace(
        loaded.config,
        n_trees=1,
        max_depth=1,
        row_subsample=1.0,
        col_subsample=1.0,
        min_samples_leaf=1,
        fault_tree_indices=(),
    )
    dataset = _tiny_dataset()
    binning = build_binning(dataset, 4)
    precision = PrecisionContext("float64")
    initial = precision.initial_binary_state(dataset.y)
    from gbdt_workbench.tree import residual_fingerprint

    residual = np.asarray(initial["residual"], dtype=precision.dtype)
    reference = np.asarray(initial["reference_residual"], dtype=np.float64)
    residual_hash = residual_fingerprint(residual, reference, precision)
    tree = train_tree(
        0,
        dataset,
        np.asarray(binning["sample_bins"], dtype=np.int32),
        binning["fingerprint"],
        residual,
        reference,
        precision,
        config,
        residual_hash,
    )
    root = tree["nodes"][0]
    assert root["kind"] == "split"
    assert root["feature_index"] == 0
    assert root["feature_name"] == "first"


def test_tree_training_is_repeatable(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    loaded = load_config(config_path)
    dataset = generate_dataset(loaded)
    binning = build_binning(dataset, loaded.config.n_bins)
    precision = PrecisionContext(loaded.config.precision)
    initial = precision.initial_binary_state(dataset.y)
    residual = np.asarray(initial["residual"], dtype=precision.dtype)
    reference = np.asarray(initial["reference_residual"], dtype=np.float64)
    from gbdt_workbench.tree import residual_fingerprint

    residual_hash = residual_fingerprint(residual, reference, precision)
    first = train_tree(
        1,
        dataset,
        np.asarray(binning["sample_bins"], dtype=np.int32),
        binning["fingerprint"],
        residual,
        reference,
        precision,
        loaded.config,
        residual_hash,
    )
    second = train_tree(
        1,
        dataset,
        np.asarray(binning["sample_bins"], dtype=np.int32),
        binning["fingerprint"],
        residual,
        reference,
        precision,
        loaded.config,
        residual_hash,
    )
    assert first["content_fingerprint"] == second["content_fingerprint"]
    assert first["nodes"] == second["nodes"]
    assert first["node_diagnostics"] == second["node_diagnostics"]
