from __future__ import annotations

import copy
from pathlib import Path

import numpy as np

from diagnostic_engine.engine import DiagnosticEngine
from diagnostic_engine.forecast import compare_quantile_vectors
from diagnostic_engine.generator import generate_fixture
from diagnostic_engine.hashing import sha256_json


def test_fixture_bytes_are_deterministic(tmp_path: Path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    generate_fixture(first, seed=440)
    generate_fixture(second, seed=440)
    assert (first / "observations.jsonl").read_bytes() == (second / "observations.jsonl").read_bytes()
    assert (first / "scenario.json").read_bytes() == (second / "scenario.json").read_bytes()


def test_pipeline_hashes_are_repeatable(config, records, tmp_path: Path):
    work_a = tmp_path / "a"
    work_b = tmp_path / "b"
    engine_a = DiagnosticEngine(copy.deepcopy(config), copy.deepcopy(records), workdir=work_a)
    engine_b = DiagnosticEngine(copy.deepcopy(config), copy.deepcopy(records), workdir=work_b)
    report_a = engine_a.run_all()
    report_b = engine_b.run_all()
    keys_a = [(w["window_id"], w["hashes"]) for w in report_a["windows"]]
    keys_b = [(w["window_id"], w["hashes"]) for w in report_b["windows"]]
    assert keys_a == keys_b
    for window in report_a["windows"]:
        for key in window["keys"].values():
            assert engine_a.cache.object_path(key).read_bytes() == engine_b.cache.object_path(key).read_bytes()


def test_precision_profiles_share_structure_and_stay_close(config, records, tmp_path: Path):
    predictions = {}
    reports = {}
    for profile in ("float64", "float32", "fixed"):
        engine = DiagnosticEngine(
            copy.deepcopy(config), copy.deepcopy(records), profile=profile, workdir=tmp_path / "runtime"
        )
        report = engine.run_all()
        reports[profile] = report
        first_key = report["windows"][0]["keys"]["predict"]
        predictions[profile] = engine.cache.get_body(first_key)["predictions"]

    base = reports["float64"]["windows"]
    for profile in ("float32", "fixed"):
        current = reports[profile]["windows"]
        assert [w["window_id"] for w in current] == [w["window_id"] for w in base]
        assert len(current[0]["keys"]) == len(base[0]["keys"])

    np.testing.assert_allclose(predictions["float64"], predictions["float32"], atol=1e-5)
    np.testing.assert_allclose(predictions["float64"], predictions["fixed"], atol=0.02)


def test_query_and_replay_match(config, records, engine):
    report = engine.run_all()
    v_idx = records[10]["v_idx"]
    queried = engine.query_v_idx(v_idx)
    assert queried["trace"]["config_version"] == config["config_version"]
    assert len(queried["trace"]["cache_keys"]) == 6

    verification = engine.verify_v_idx(v_idx)
    assert verification["ok"] is True
    assert verification["checks"] == [
        "config_hash",
        "cache_checksums",
        "prediction_recompute",
        "error_spectrum_recompute",
    ]
    assert report["verification"]["ok"] is True


def test_stale_config_result_is_rejected(config, records, engine):
    engine.run_all()
    v_idx = records[0]["v_idx"]
    engine.config["config_version"] = "scenario-changed"
    try:
        engine.verify_v_idx(v_idx)
    except Exception as exc:
        assert exc.code == "E_STALE_RESULT"
    else:
        raise AssertionError("expected E_STALE_RESULT")


def test_replay_hash_is_content_stable(config, records, workdir):
    engine = DiagnosticEngine(config, records, workdir=workdir)
    engine.run_all()
    v_idx = records[0]["v_idx"]
    trace = engine.query_v_idx(v_idx)["trace"]
    predict_body = engine.cache.get_body(trace["cache_keys"]["predict"])
    first_hash = sha256_json(predict_body["predictions"])
    second_hash = sha256_json(engine.cache.get_body(trace["cache_keys"]["predict"])["predictions"])
    assert first_hash == second_hash


def test_strict_and_tolerance_quantile_comparison():
    left = np.asarray([[0.1, 0.5, 0.9]], dtype=np.float64)
    right = np.asarray([[0.1, 0.504, 0.9]], dtype=np.float64)
    strict = compare_quantile_vectors(left, right, strategy="strict")
    tolerance = compare_quantile_vectors(
        left, right, strategy="tolerance", tolerance=0.01, near_band=0.01
    )
    assert strict["passed"] is False
    assert tolerance["passed"] is True
    assert tolerance["near_boundary_samples"]
