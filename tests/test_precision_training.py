"""Requirements 2 and 3: multi-precision core and chunk/whole comparison."""

from __future__ import annotations

import numpy as np
import pytest

from seraph_core.config.config import EngineConfig
from seraph_core.data.dataset import read_manifest
from seraph_core.diag.error_spectrum import (chunk_vs_whole_report,
                                             run_all_variants)
from seraph_core.engine import pipeline as pl
from seraph_core.engine.precision import (FixedBackend, Float32Backend,
                                          make_backend, round_array)
from seraph_core.engine.training import (LinearRegressor, chunk_plan,
                                         enforce_resource_limits)
from seraph_core.errors import EngineError


def _dataset(config, work_dir):
    manifest = read_manifest(f"{work_dir['data']}/manifest.jsonl")
    outputs = [pl.process_dialogue(
        pl.load_dialogue(work_dir["data"], entry, config.frame_length,
                         config.hop_length), config) for entry in manifest]
    return pl.pool_from_framing(outputs, config)


def test_rounding_modes_are_explicit():
    values = np.array([-1.5, -0.5, 0.5, 1.5, 2.5])
    assert round_array(values, "half_even").tolist() == [-2.0, 0.0, 0.0,
                                                          2.0, 2.0]
    assert round_array(values, "half_up").tolist() == [-1.0, 0.0, 1.0,
                                                        2.0, 3.0]
    assert round_array(values, "truncate").tolist() == [-1.0, 0.0, 0.0,
                                                         1.0, 2.0]


def test_fixed_backend_uses_scaled_integers(base_config):
    base_config.precision = "fixed"
    base_config.fixed_scale = 1000
    backend = FixedBackend(base_config)
    fixed = backend.to_fixed(np.array([0.123456, -0.654321]))
    assert fixed.dtype == np.int64
    assert fixed.tolist() == [123, -654]
    recovered = backend.cast(np.array([0.123456]))
    assert recovered[0] == 0.123


def test_backend_factory_covers_three_tiers(base_config):
    assert make_backend(base_config).name == "float64"
    base_config.precision = "float32"
    assert isinstance(make_backend(base_config), Float32Backend)
    base_config.precision = "fixed"
    assert isinstance(make_backend(base_config), FixedBackend)


def test_chunk_plan_is_deterministic_and_contiguous():
    plan = chunk_plan(10, 3)
    assert plan == [(0, 3), (3, 6), (6, 9), (9, 10)]
    assert chunk_plan(10, 0) == [(0, 10)]


def test_same_config_reproduces_bit_identical_weights(base_config, work_dir):
    dataset = _dataset(base_config, work_dir)
    plan = chunk_plan(dataset["x_aug"].shape[0], base_config.chunk_size)

    model_a = LinearRegressor(dataset["x_aug"].shape[1] - 1, base_config)
    history_a, _ = model_a.train(dataset["x_aug"], dataset["y"], plan)
    model_b = LinearRegressor(dataset["x_aug"].shape[1] - 1, base_config)
    history_b, _ = model_b.train(dataset["x_aug"], dataset["y"], plan)
    assert np.array_equal(model_a.weights, model_b.weights)
    assert [row.loss for row in history_a] == [row.loss for row in history_b]


def test_precision_switch_only_changes_arithmetic(base_config, work_dir):
    dataset = _dataset(base_config, work_dir)
    plan = chunk_plan(dataset["x_aug"].shape[0], 4)
    losses = {}
    for precision in ("float64", "float32", "fixed"):
        config = EngineConfig(**{**base_config.to_dict(),
                                 "precision": precision})
        model = LinearRegressor(dataset["x_aug"].shape[1] - 1, config)
        history, _ = model.train(dataset["x_aug"], dataset["y"], plan)
        losses[precision] = [row.loss for row in history]
    assert len(losses["float64"]) == len(losses["float32"])
    assert losses["float64"] != losses["fixed"]


def test_chunk_vs_whole_report_structure(base_config, work_dir):
    dataset = _dataset(base_config, work_dir)
    variants = run_all_variants(dataset, base_config)
    report = chunk_vs_whole_report(base_config, variants=variants)
    assert report["per_epoch"][0]["max_abs_loss_deviation"] >= 0.0
    assert "final_parameter_deviation" in report
    assert "first_divergence_epoch" in report
    assert "convergence_diverged" in report
    assert report["final_parameter_deviation"]["max_abs"] > 0.0


def test_resource_limit_raises_and_degrades(tiny_limits, work_dir):
    dataset = _dataset(tiny_limits, work_dir)
    n_features = dataset["x_aug"].shape[1] - 1
    chunk_size, degradations = enforce_resource_limits(
        2, dataset["x_aug"].shape[0], n_features, tiny_limits)
    assert chunk_size >= tiny_limits.resource_limits.min_chunk_size
    tiny_limits.resource_limits.max_dialogs = 1
    with pytest.raises(EngineError) as exc:
        enforce_resource_limits(2, dataset["x_aug"].shape[0], n_features,
                                tiny_limits)
    assert exc.value.code == "E_RESOURCE_LIMIT"


def test_gradient_degradation_is_marked(tiny_limits, work_dir):
    dataset = _dataset(tiny_limits, work_dir)
    n_features = dataset["x_aug"].shape[1] - 1
    rng = np.random.default_rng(3)
    big_x = rng.normal(size=(128, n_features))
    big_y = rng.normal(size=(128, 2))
    tiny_limits.chunk_size = 32
    tiny_limits.resource_limits.max_gradient_bytes = 4096
    chunk_size, degradations = enforce_resource_limits(
        2, big_x.shape[0], n_features, tiny_limits)
    assert chunk_size < 32
    assert degradations[0]["category"] == "chunk_size_downgrade"
