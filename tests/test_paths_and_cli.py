from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pose_pipeline.cli import build_experiment, main
from pose_pipeline.errors import PipelineError
from pose_pipeline.pipeline import run_pipeline
from tests.helpers import make_config, make_dataset


def test_path_drift_is_explicit(tmp_path):
    config = make_config(num_images=1, num_keypoints=5, candidates=6)
    config["adaptive"]["max_peak_comparisons_per_image"] = 30_000
    config["adaptive"]["reference_max_peak_comparisons_per_image"] = 1_000_000
    config["adaptive"]["path_tolerance_px"] = 0.01
    samples, manifest_dir = make_dataset(tmp_path, config)
    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(samples, config, manifest_dir)
    assert exc_info.value.code == "E_PATH_DRIFT"
    assert exc_info.value.image_id == samples[0]["image_id"]
    violations = exc_info.value.details["violations"]
    assert violations
    assert {"channel_index", "exact_position", "approx_position", "distance_px"} <= set(
        violations[0]
    )


def test_reference_unavailable_is_explicit_not_hidden(tmp_path):
    config = make_config(num_images=1, num_keypoints=5, candidates=6)
    config["adaptive"]["max_peak_comparisons_per_image"] = 30_000
    config["adaptive"]["reference_max_candidates_per_image"] = 5
    samples, manifest_dir = make_dataset(tmp_path, config)
    report = run_pipeline(samples, config, manifest_dir)
    image = report["images"][0]
    assert image["decision"]["selected_path"] == "approx"
    assert image["decision"]["exact_reference"]["available"] is False
    assert image["cross_path"]["available"] is False
    assert image["cross_path"]["reasons"][0]["code"] == "REFERENCE_CANDIDATE_LIMIT"
    assert report["summary"]["exact_reference_unavailable_count"] == 1
    assert report["self_check"]["ok"] is True


def test_pipeline_covers_every_image_and_hashes_are_reproducible(tmp_path):
    config = make_config(
        num_images=3,
        num_keypoints=[5, 7, 6],
        candidates=[2, 9, 4],
        skeletons=[1, 2, 1],
    )
    config["adaptive"]["exact_max_images"] = 1
    samples, manifest_dir = make_dataset(tmp_path, config)
    first = run_pipeline(samples, config, manifest_dir)
    second = run_pipeline(samples, config, manifest_dir)
    assert first["summary"]["covered_image_count"] == 3
    assert first["summary"]["selected_path_counts"] == {"exact": 0, "approx": 3}
    assert first["report_sha256"] == second["report_sha256"]
    assert first["self_check"]["ok"] is True


def test_cli_experiment_reproduces_complete_output(tmp_path):
    baseline_path = tmp_path / "baseline.json"
    scale_path = tmp_path / "scale.json"
    baseline = make_config(
        num_images=1,
        num_keypoints=5,
        candidates=2,
        skeletons=1,
    )
    baseline["image_prefix"] = "tiny-baseline"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    scale = make_config(
        num_images=2,
        num_keypoints=5,
        candidates=[2, 9],
        skeletons=1,
    )
    scale["image_prefix"] = "tiny-scale"
    scale["adaptive"]["exact_max_images"] = 1
    scale_path.write_text(json.dumps(scale), encoding="utf-8")
    report = build_experiment(baseline_path, scale_path, tmp_path / "data")
    assert report["status"] == "ok"
    assert report["reproducibility"]["all_matched"] is True
    assert report["self_check"]["ok"] is True
    assert report["content_sha256"]


def test_cli_writes_json_error_report_for_invalid_threshold(tmp_path):
    bad_config = make_config(num_images=1, candidates=2)
    bad_config["matching"]["threshold"] = 0.0
    config_path = tmp_path / "bad.json"
    config_path.write_text(json.dumps(bad_config), encoding="utf-8")
    out_path = tmp_path / "error.json"
    code = main(
        [
            "--baseline-config",
            str(config_path),
            "--scale-config",
            str(config_path),
            "--data-root",
            str(tmp_path / "data"),
            "--out",
            str(out_path),
        ]
    )
    assert code == 2
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    assert report["business_codes"][0]["code"] == "E_THRESHOLD_INVALID"
