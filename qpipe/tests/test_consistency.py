"""Requirement 8: cross-module consistency self-checks."""

from __future__ import annotations

import pytest

from qpipe import E_INCONSISTENT, QuantPipeline, RunContext, samplegen
from qpipe.consistency import (
    check_independent_prediction,
    check_layer_sets,
    check_quant_roundtrip,
    check_recompute_reuse_partition,
    run_all_checks,
)


def test_all_checks_pass_on_clean_run(generated, package):
    pipeline = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"],
        context=RunContext(seed=generated["seed"]),
    )
    pipeline.run_all()
    calibration = samplegen.load_calibration(pipeline._calibration_path)
    report = run_all_checks(
        package, pipeline.store, pipeline.cache, calibration
    )
    assert report["ok"] is True
    assert set(report["checks"]) == {
        "layer_sets",
        "quant_roundtrip",
        "recompute_reuse",
        "independent_prediction",
    }


def test_recompute_reuse_partition_detected(generated):
    point = {
        "completed_layers": [0, 1, 2, 3],
        "recomputed_layers": [2, 3],
        "reused_layers": [0, 1],
        "affected_layers": [2, 3],
    }
    ok, _ = check_recompute_reuse_partition(point)
    assert ok is True

    bad = dict(point)
    bad["reused_layers"] = [1, 2]  # overlap on layer 2
    with pytest.raises(Exception) as exc:
        check_recompute_reuse_partition(bad)
    assert exc.value.code == E_INCONSISTENT
    assert exc.value.detail["category"] == "recompute_reuse"


def test_layer_set_mismatch_raises(generated, package):
    pipeline = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"],
        context=RunContext(seed=generated["seed"]),
    )
    pipeline.run_all()
    pipeline.cache.remove_layer("infer", 3)
    with pytest.raises(Exception) as exc:
        check_layer_sets(package, pipeline.cache)
    assert exc.value.code == E_INCONSISTENT


def test_quant_roundtrip_and_independent_prediction(generated, package):
    pipeline = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"],
        context=RunContext(seed=generated["seed"]),
    )
    pipeline.run_all()
    ok, _ = check_quant_roundtrip(package, pipeline.cache)
    assert ok is True
    calibration = samplegen.load_calibration(pipeline._calibration_path)
    ok, payload = check_independent_prediction(
        package, pipeline.cache, calibration
    )
    assert ok is True
    assert len(payload["prediction_checksum"]) == 64
