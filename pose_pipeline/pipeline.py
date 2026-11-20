from __future__ import annotations

from pathlib import Path
from typing import Any

from .adaptive import decide_path
from .config import validate_config
from .crosscheck import compare_paths, unavailable_reference
from .errors import PipelineError
from .grouping import group_peaks
from .heatmap import decode_heatmaps
from .matching import (
    candidate_instances,
    grouped_instances,
    match_instances,
    truth_instances,
)
from .metrics import aggregate_metrics, compute_metrics
from .samples import verify_saved_heatmaps
from .selfcheck import self_check_report
from .utils import json_sha256, round_float
from .validation import validate_samples


def _run_path(
    sample: dict[str, Any],
    heatmaps: Any,
    path: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    decoded = decode_heatmaps(heatmaps, sample, path, config["heatmap"])
    grouped = group_peaks(decoded, sample, path, config["heatmap"])
    truths = truth_instances(sample)
    predictions = grouped_instances(grouped)
    matching_config = dict(config["matching"])
    matching_config["approx_chunk_size"] = int(config["adaptive"]["approx_chunk_size"])
    strategy = (
        config["adaptive"]["exact_match_strategy"]
        if path == "exact"
        else config["adaptive"]["approx_match_strategy"]
    )
    matching = match_instances(
        predictions,
        truths,
        sample,
        matching_config,
        strategy,
    )
    metrics = compute_metrics(
        sample,
        truths,
        predictions,
        matching,
        matching_config,
    )
    candidate_predictions = candidate_instances(sample)
    candidate_matching = match_instances(
        candidate_predictions,
        truths,
        sample,
        matching_config,
        strategy,
    )
    best_candidates = []
    for assignment in candidate_matching["assignments"]:
        best_candidates.append(
            {
                "truth_instance_id": assignment["truth_instance_id"],
                "candidate_id": assignment["predicted_instance_id"],
                "candidate_source": assignment["prediction_source"],
                "score": assignment["score"],
                "accepted": assignment["accepted"],
            }
        )
    candidate_adjudication = {
        "strategy": strategy,
        "matching": candidate_matching,
        "best_candidate_by_truth": best_candidates,
    }
    return {
        "path": path,
        "decoded": decoded,
        "grouped": grouped,
        "matching": matching,
        "metrics": metrics,
        "candidate_adjudication": candidate_adjudication,
    }


def _image_business_codes(decision: dict[str, Any], cross_path: dict[str, Any]) -> list[dict[str, Any]]:
    codes = []
    if decision["degraded"]:
        codes.append(
            {
                "code": "DEGRADED_PATH",
                "severity": "warning",
                "field": "decision.selected_path",
                "image_id": decision["image_id"],
                "message": "exact path exceeded a configured scale or resource budget; approximate path selected",
                "details": {
                    "categories": decision["degradation_categories"],
                    "reasons": decision["reasons"],
                },
            }
        )
    if not cross_path["available"]:
        codes.append(
            {
                "code": "REFERENCE_UNAVAILABLE",
                "severity": "warning",
                "field": "cross_path.available",
                "image_id": decision["image_id"],
                "message": "exact reference was not computed because the declared scale budget forbids it",
                "details": {"reasons": cross_path["reasons"]},
            }
        )
    return codes


def run_pipeline(
    samples: list[dict[str, Any]],
    config: dict[str, Any],
    manifest_dir: str | Path,
) -> dict[str, Any]:
    validate_config(config)
    validate_samples(samples, config)
    if len(samples) != int(config["num_images"]):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "num_images",
            "manifest image count does not match config.num_images",
            details={"manifest": len(samples), "config": int(config["num_images"])},
        )

    image_reports = []
    business_codes = [
        {
            "code": "OK",
            "severity": "info",
            "field": "pipeline",
            "message": "pipeline completed all declared processing stages",
        }
    ]
    selected_path_counts = {"exact": 0, "approx": 0}
    total_peak_comparisons = 0
    total_candidate_matrix_cells = 0
    reference_available_count = 0
    reference_unavailable_count = 0
    max_cross_delta = 0.0

    for sample in samples:
        decision = decide_path(sample, len(samples), config)
        if decision["selected_path"] == "exact" and not decision["exact_reference"]["available"]:
            raise PipelineError(
                "E_INCONSISTENT",
                "cross_path.exact_reference",
                "exact path was selected while the exact reference was declared unavailable",
                image_id=sample["image_id"],
                category="REFERENCE_DECLARATION",
            )
        heatmaps = verify_saved_heatmaps(
            sample,
            manifest_dir,
            float(config["heatmap_sigma"]),
        )
        selected_result = _run_path(
            sample,
            heatmaps,
            decision["selected_path"],
            config,
        )
        exact_result = selected_result if decision["selected_path"] == "exact" else None
        approx_result = selected_result if decision["selected_path"] == "approx" else None

        if decision["exact_reference"]["available"]:
            if exact_result is None:
                exact_result = _run_path(sample, heatmaps, "exact", config)
            if approx_result is None:
                approx_result = _run_path(sample, heatmaps, "approx", config)
            cross_path = compare_paths(
                exact_result,
                approx_result,
                sample,
                float(config["adaptive"]["path_tolerance_px"]),
            )
            reference_available_count += 1
            max_cross_delta = max(
                max_cross_delta,
                float(cross_path["max_coordinate_delta_px"]),
            )
        else:
            if decision["selected_path"] == "exact":
                raise PipelineError(
                    "E_INCONSISTENT",
                    "cross_path.exact_reference",
                    "selected exact path has no exact reference",
                    image_id=sample["image_id"],
                    category="REFERENCE_DECLARATION",
                )
            cross_path = unavailable_reference(
                decision["exact_reference"]["reasons"]
            )
            reference_unavailable_count += 1

        exact_estimate = decision["estimates"]["peak_comparisons"]["exact"]
        approx_estimate = decision["estimates"]["peak_comparisons"]["approx"]
        exact_memory = decision["estimates"]["memory_mb"]["exact"]
        approx_memory = decision["estimates"]["memory_mb"]["approx"]
        if cross_path["available"]:
            exact_pck = cross_path["overall_pck"]["exact"]
            approx_pck = cross_path["overall_pck"]["approx"]
        else:
            exact_pck = None
            approx_pck = None
        decision["adjudication_outcome"] = {
            "selected_path": decision["selected_path"],
            "selected_path_pck": selected_result["metrics"]["pck"],
            "exact_reference_pck": exact_pck,
            "approx_pck": approx_pck,
            "observed_precision_gain_exact_minus_approx": (
                round_float(float(exact_pck) - float(approx_pck))
                if exact_pck is not None and approx_pck is not None
                else None
            ),
            "estimated_peak_comparison_saving_approx_vs_exact": int(
                exact_estimate - approx_estimate
            ),
            "estimated_memory_saving_mb_approx_vs_exact": round_float(
                exact_memory - approx_memory
            ),
            "metric_contract": "identical PCK/OKS thresholds for exact and approximate paths",
            "priority": list(config["adaptive"]["priority"]),
            "basis": (
                "hard safety limits first, then complete progress, then precision; "
                "degrade only when the exact path violates a declared budget"
            ),
        }

        image_codes = _image_business_codes(decision, cross_path)
        business_codes.extend(image_codes)
        selected_path_counts[decision["selected_path"]] += 1
        total_peak_comparisons += int(selected_result["decoded"]["comparison_count"])
        total_candidate_matrix_cells += int(
            selected_result["candidate_adjudication"]["matching"]["matrix_cells"]
        )

        image_report = {
            "image_id": sample["image_id"],
            "canvas_shape": list(sample["canvas_shape"]),
            "num_keypoints": sample["num_keypoints"],
            "occlusion_ratio": sample["occlusion_ratio"],
            "heatmap_seed": sample["heatmap_seed"],
            "heatmap_sha256": sample["heatmap_sha256"],
            "truth_heatmap_sha256": sample["truth_heatmap_sha256"],
            "target_heatmap_sha256": sample["target_heatmap_sha256"],
            "num_channels": sample["num_channels"],
            "summary": {
                "skeleton_count": len(sample["skeletons"]),
                "candidate_count": len(sample["candidates"]),
                "keypoint_count": len(sample["skeletons"]) * sample["num_keypoints"],
            },
            "decision": decision,
            "selected_path": decision["selected_path"],
            "selected_path_result": selected_result,
            "cross_path": cross_path,
            "business_codes": image_codes,
            "coverage": {
                "image_processed": True,
                "selected_path_covered": True,
                "all_skeleton_keypoints_covered": len(
                    selected_result["grouped"]["assigned_peak_ids"]
                )
                == len(sample["skeletons"]) * sample["num_keypoints"],
            },
        }
        image_reports.append(image_report)

    summary = {
        "image_count": len(samples),
        "covered_image_count": sum(
            1
            for image in image_reports
            if image["coverage"]["selected_path_covered"]
        ),
        "selected_path_counts": selected_path_counts,
        "degraded_image_count": sum(
            1 for image in image_reports if image["decision"]["degraded"]
        ),
        "exact_reference_available_count": reference_available_count,
        "exact_reference_unavailable_count": reference_unavailable_count,
        "max_cross_path_coordinate_delta_px": round_float(max_cross_delta),
        "total_selected_peak_comparisons": total_peak_comparisons,
        "total_candidate_matching_matrix_cells": total_candidate_matrix_cells,
        "metrics": aggregate_metrics(
            [image["selected_path_result"]["metrics"] for image in image_reports]
        ),
    }
    report = {
        "schema_version": "1.0",
        "experiment": {
            "seed": int(config["seed"]),
            "image_prefix": str(config["image_prefix"]),
            "canvas_shape": list(config["canvas_shape"]),
            "matching": dict(config["matching"]),
            "adaptive_policy": dict(config["adaptive"]),
        },
        "images": image_reports,
        "summary": summary,
        "business_codes": business_codes,
    }
    self_check = self_check_report(report, config)
    report["self_check"] = self_check
    report["business_codes"].extend(self_check["issues"])
    report["status"] = "ok" if self_check["ok"] else "failed"
    report["business_code_counts"] = {}
    for item in report["business_codes"]:
        code = item["code"]
        report["business_code_counts"][code] = (
            report["business_code_counts"].get(code, 0) + 1
        )
    report["report_sha256"] = json_sha256(report)
    return report
