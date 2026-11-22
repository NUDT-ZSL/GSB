from __future__ import annotations

import numpy as np
import pytest

from table_features.config import parse_config
from table_features.errors import E_CONFIG_INVALID, BusinessError
from table_features.pipeline import Pipeline, write_result_artifacts


def test_encoding_and_standardization_are_byte_reproducible(tmp_path, rows, cfg):
    first = Pipeline(tmp_path / "cache-1", tmp_path / "snap-1", clear=True).run(rows, cfg)
    second = Pipeline(tmp_path / "cache-2", tmp_path / "snap-2", clear=True).run(rows, cfg)
    assert first.matrix.tobytes() == second.matrix.tobytes()
    assert first.columns == second.columns
    assert first.matrix_digest == second.matrix_digest
    assert first.selected_features == second.selected_features
    assert first.scores == second.scores

    write_result_artifacts(first, tmp_path / "out-1")
    write_result_artifacts(second, tmp_path / "out-2")
    for filename in (
        "matrix.npy",
        "standardization.json",
        "encoding.json",
        "cross.json",
        "selection.json",
    ):
        assert (tmp_path / "out-1" / filename).read_bytes() == (
            tmp_path / "out-2" / filename
        ).read_bytes()


def test_target_encoding_records_folds_formula_and_smoothing(tmp_path, rows, cfg):
    result = Pipeline(
        tmp_path / "target-cache",
        tmp_path / "target-snapshot",
        clear=True,
    ).run(rows, cfg)
    region = next(block for block in result.category_blocks if block.logical_feature == "region")
    params = region.params
    assert params["encoding"] == "target"
    assert params["smoothing"] == 8.0
    assert params["folds"] == 4
    assert params["seed"] == 101
    assert sum(params["fold_sizes"]) == len(rows)
    assert len(params["fold_assignment"]) == len(rows)
    assert "sum_y_other_folds" in params["formula"]
    assert params["fold_rule"].startswith("permutation")
    assert len(params["global_mean_other_folds"]) == len(rows)


def test_zscore_and_minmax_parameters_are_exact(tmp_path, rows, cfg):
    result = Pipeline(
        tmp_path / "std-cache",
        tmp_path / "std-snapshot",
        clear=True,
    ).run(rows, cfg)
    by_name = {block.logical_feature: block for block in result.numeric_blocks}
    ages = np.asarray([row["age"] for row in rows], dtype=float)
    incomes = np.asarray([row["income"] for row in rows], dtype=float)
    age_params = by_name["age"].params
    income_params = by_name["income"].params
    assert age_params["mean"] == float(np.mean(ages))
    assert age_params["std"] == float(np.std(ages, ddof=0))
    assert np.allclose(
        by_name["age"].matrix[:, 0],
        (ages - np.mean(ages)) / np.std(ages, ddof=0),
    )
    assert income_params["min"] == float(np.min(incomes))
    assert income_params["max"] == float(np.max(incomes))
    assert income_params["standardization"] == "min-max"
    assert np.allclose(
        by_name["income"].matrix[:, 0],
        (incomes - np.min(incomes)) / (np.max(incomes) - np.min(incomes)),
    )


@pytest.mark.parametrize(
    ("path", "bad_value", "stage", "feature", "field"),
    [
        (
            ("category_features", 0, "encoding"),
            "frequency",
            "encode",
            "color",
            "category_features[0].encoding",
        ),
        (
            ("numeric_features", 0, "standardization"),
            "robust",
            "standardize",
            "age",
            "numeric_features[0].standardization",
        ),
    ],
)
def test_unknown_encoding_or_standardization_is_classified(cfg, path, bad_value, stage, feature, field):
    raw = dict(cfg.raw)
    import copy

    raw = copy.deepcopy(raw)
    node = raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = bad_value
    with pytest.raises(BusinessError) as caught:
        parse_config(raw)
    assert caught.value.record.code == E_CONFIG_INVALID
    assert caught.value.record.stage == stage
    assert caught.value.record.feature == feature
    assert caught.value.record.details["field"] == field

