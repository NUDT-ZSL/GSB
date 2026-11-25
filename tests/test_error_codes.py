from __future__ import annotations

import copy

import numpy as np
import pytest

from diagnostic_engine.config import validate_config
from diagnostic_engine.cross import standardize_cross_features
from diagnostic_engine.engine import DiagnosticEngine
from diagnostic_engine.errors import DiagnosticError
from diagnostic_engine.forecast import _drift_statistics, predict_quantiles
from diagnostic_engine.precision import Precision
from diagnostic_engine.preprocessing import assert_window_not_regressed, validate_records


def test_duplicate_v_idx_is_bad_input(records, config):
    bad = copy.deepcopy(records[:2])
    bad[1]["v_idx"] = bad[0]["v_idx"]
    with pytest.raises(DiagnosticError) as exc:
        validate_records(bad, config)
    assert exc.value.code == "E_BAD_INPUT"


def test_window_regression_is_bad_input():
    with pytest.raises(DiagnosticError) as exc:
        assert_window_not_regressed(10_000, 9_000)
    assert exc.value.code == "E_BAD_INPUT"


def test_unknown_source_name_is_bad_input(records, config):
    bad = copy.deepcopy(records[:1])
    bad[0]["src_name"] = "does_not_exist"
    with pytest.raises(DiagnosticError) as exc:
        validate_records(bad, config)
    assert exc.value.code == "E_BAD_INPUT"


def test_missing_cross_variable_is_cross_def(config):
    config["cross"]["slots"][0]["variables"].append("missing_var")
    with pytest.raises(DiagnosticError) as exc:
        validate_config(config)
    assert exc.value.code == "E_CROSS_DEF"


def test_cross_column_limit_is_mem_limit(engine, config):
    engine.config["cross"]["max_cross_columns"] = 1
    with pytest.raises(DiagnosticError) as exc:
        engine.run_all()
    assert exc.value.code == "E_MEM_LIMIT"


def test_zero_standardization_denominator(config):
    precision = Precision.from_config(config)
    cross_result = {
        "expanded_matrix": np.zeros((32, 1)),
        "columns": ["pair#x00-x01#product"],
        "metadata": [
            {"column": "pair#x00-x01#product", "condition_var": "x00"}
        ],
    }
    preprocess_result = {
        "variables": config["variables"],
        "matrix": np.tile(np.linspace(-1, 1, 32)[:, None], (1, 10)),
    }
    with pytest.raises(DiagnosticError) as exc:
        standardize_cross_features(cross_result, preprocess_result, config, precision)
    assert exc.value.code == "E_STD_ZERO"


def test_illegal_intermediate_dim(config):
    nested = next(c for c in config["reduction"]["candidates"] if c["name"] == "nested")
    nested["intermediate_dim"] = nested["dim"] - 1
    with pytest.raises(DiagnosticError) as exc:
        validate_config(config)
    assert exc.value.code == "E_INTERMEDIATE_DIM"


def test_drift_missing_for_short_series(config):
    precision = Precision.from_config(config)
    preprocess = {
        "variables": ["x00"],
        "stats": {"x00": {"mean": 0.0}},
    }
    with pytest.raises(DiagnosticError) as exc:
        _drift_statistics(np.asarray([0.1]), preprocess, config, precision, None)
    assert exc.value.code == "E_DRIFT_MISSING"


def test_quantile_order_error(config):
    precision = Precision.from_config(config)
    model = {
        "quantiles": [0.9, 0.1, 0.5],
        "coefficients": [[0.1, 0.2, 0.3]] * 3,
        "intercepts": [0.0, 0.0, 0.0],
    }
    with pytest.raises(DiagnosticError) as exc:
        predict_quantiles(np.zeros((4, 3)), model, precision)
    assert exc.value.code == "E_QUANTILE_ORDER"


def test_unknown_precision_and_nonpositive_scale(config):
    with pytest.raises(DiagnosticError) as exc:
        Precision.from_config(config, "int8-ish")
    assert exc.value.code == "E_PRECISION_INVALID"

    bad = copy.deepcopy(config)
    bad["precision"]["profiles"]["fixed"]["scale"] = 0
    with pytest.raises(DiagnosticError) as exc:
        Precision.from_config(bad, "fixed")
    assert exc.value.code == "E_PRECISION_INVALID"


def test_engine_uses_declared_profile(engine):
    assert engine.precision.name == "float64"
    fixed_engine = DiagnosticEngine(engine.config, [], profile="fixed", workdir=engine.workdir)
    assert fixed_engine.precision.scale == 1000
