from __future__ import annotations

import itertools
import math

import pytest

from pose_pipeline.matching import (
    _hungarian_min,
    match_instances,
    pair_similarity,
)
from pose_pipeline.metrics import recompute_from_keypoint_details
from pose_pipeline.pipeline import run_pipeline
from tests.helpers import make_config, make_dataset


def _prediction(instance_id, source, x, y, keypoint_index=0):
    return {
        "instance_id": instance_id,
        "source": source,
        "skeleton_id": 0,
        "keypoints": {
            keypoint_index: {
                "keypoint_index": keypoint_index,
                "x": x,
                "y": y,
                "peak_id": f"{instance_id}:kp{keypoint_index}",
            }
        },
    }


def _truth(instance_id, x, y, keypoint_index=0, visible=True):
    return {
        "instance_id": instance_id,
        "source": "gt",
        "skeleton_id": 0,
        "keypoints": {
            keypoint_index: {
                "index": keypoint_index,
                "x": x,
                "y": y,
                "visible": visible,
                "confidence": 1.0,
            }
        },
    }


def _sample(num_keypoints=1):
    return {
        "image_id": "unit",
        "canvas_shape": [32, 32],
        "num_keypoints": num_keypoints,
        "keypoint_meta": [
            {"index": index, "name": f"kp{index}", "oks_sigma": 0.05}
            for index in range(num_keypoints)
        ],
    }


def _brute_force_min(cost):
    rows = len(cost)
    columns = len(cost[0])
    best = math.inf
    if rows <= columns:
        for columns_choice in itertools.permutations(range(columns), rows):
            best = min(
                best,
                sum(cost[row][column] for row, column in enumerate(columns_choice)),
            )
    else:
        for rows_choice in itertools.permutations(range(rows), columns):
            best = min(
                best,
                sum(cost[row][column] for column, row in enumerate(rows_choice)),
            )
    return best


def test_hungarian_matches_brute_force_for_rectangular_matrices():
    matrices = [
        [[1.0, 2.0, 3.0], [3.0, 1.0, 2.0]],
        [[2.0, 1.0], [1.0, 2.0], [3.0, 3.0]],
        [[4.0, 1.0, 3.0], [2.0, 0.0, 5.0], [3.0, 2.0, 2.0]],
    ]
    for matrix in matrices:
        assignment = _hungarian_min(matrix)
        if len(matrix) <= len(matrix[0]):
            total = sum(
                matrix[row][column]
                for row, column in enumerate(assignment)
                if column >= 0
            )
        else:
            total = sum(
                matrix[row][column]
                for column, row in enumerate(assignment)
                if row >= 0
            )
        assert total == pytest.approx(_brute_force_min(matrix))


def test_matching_tie_break_prefers_source_then_keypoint_index():
    sample = _sample()
    prediction_beta = _prediction("pred-b", "candidate_beta", 10.1, 10.0)
    prediction_alpha = _prediction("pred-a", "candidate_alpha", 10.0, 10.0)
    truth = _truth("truth-0", 10.0, 10.0)
    matching_config = {
        "metric": "pck",
        "threshold": 0.5,
        "pck_threshold_px": 1.5,
        "exclude_invisible": True,
        "oks_sigma_scale": 0.05,
        "approx_chunk_size": 1,
    }
    matching = match_instances(
        [prediction_beta, prediction_alpha],
        [truth],
        sample,
        matching_config,
        "chunked_greedy",
    )
    assignment = matching["assignments"][0]
    assert assignment["predicted_instance_id"] == "pred-a"
    assert assignment["prediction_source"] == "candidate_alpha"


def test_greedy_chunks_do_not_reuse_a_prediction():
    sample = _sample()
    predictions = [
        _prediction("pred-0", "candidate_alpha", 10.0, 10.0),
        _prediction("pred-1", "candidate_beta", 10.1, 10.0),
    ]
    truths = [
        _truth("truth-0", 10.0, 10.0),
        _truth("truth-1", 10.1, 10.0),
    ]
    matching_config = {
        "metric": "pck",
        "threshold": 0.5,
        "pck_threshold_px": 1.5,
        "exclude_invisible": True,
        "oks_sigma_scale": 0.05,
        "approx_chunk_size": 1,
    }
    matching = match_instances(
        predictions,
        truths,
        sample,
        matching_config,
        "chunked_greedy",
    )
    assigned = [
        item["predicted_instance_id"]
        for item in matching["assignments"]
        if item["accepted"]
    ]
    assert len(assigned) == len(set(assigned)) == 2


def test_pck_metrics_recompute_and_exclude_invisible(tmp_path):
    config = make_config(num_images=1, num_keypoints=5, candidates=3)
    samples, manifest_dir = make_dataset(tmp_path, config)
    sample = samples[0]
    sample["skeletons"][0]["keypoints"][0]["visible"] = False
    sample["heatmap_sha256"] = sample["heatmap_sha256"]
    from pose_pipeline.samples import synthesize_heatmaps
    import numpy as np

    heatmaps = synthesize_heatmaps(sample, config["heatmap_sigma"])
    from pose_pipeline.samples import _array_sha256

    sample["heatmap_sha256"] = _array_sha256(heatmaps)
    sample["truth_heatmap_sha256"] = _array_sha256(heatmaps[0])
    sample["target_heatmap_sha256"] = _array_sha256(heatmaps[1])
    np.save(manifest_dir / sample["heatmap_file"], heatmaps)

    report = run_pipeline(samples, config, manifest_dir)
    metrics = report["images"][0]["selected_path_result"]["metrics"]
    recomputed = recompute_from_keypoint_details(metrics["keypoint_details"])
    assert metrics["total_keypoints"] == 5
    assert metrics["excluded_invisible_keypoints"] == 1
    assert metrics["evaluated_keypoints"] == 4
    assert recomputed["visibility_denominator_consistent"] is True
    assert recomputed["pck"] == metrics["pck"]
    assert report["self_check"]["ok"] is True


def test_metric_tamper_is_reported_as_inconsistent(tmp_path):
    config = make_config(num_images=1, num_keypoints=5, candidates=3)
    samples, manifest_dir = make_dataset(tmp_path, config)
    report = run_pipeline(samples, config, manifest_dir)
    report["images"][0]["selected_path_result"]["metrics"]["pck_hits"] += 1
    from pose_pipeline.selfcheck import self_check_report

    check = self_check_report(report, config)
    assert check["ok"] is False
    categories = {issue["category"] for issue in check["issues"]}
    assert "METRIC_MISMATCH" in categories
    assert all(issue["code"] == "E_INCONSISTENT" for issue in check["issues"])
