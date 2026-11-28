from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .generator import GeneratedProject
from .storage import read_json, sha256_bytes, write_json_atomic

NORMALIZED_FLOAT_TOLERANCE = 2.0e-9
RENDER_FLOAT_TOLERANCE = 1.0e-8
PERCEPTUAL_CHANNEL_THRESHOLD = 1.0 / 255.0


def _quaternion_canonical(values: list[float]) -> np.ndarray:
    q = np.asarray(values, dtype=np.float64)
    if q[0] < 0.0:
        q = -q
    return q


def _semantic_element(element: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": element["id"],
        "layer_index": int(element["layer_index"]),
        "layer_name": element["layer_name"],
        "center": np.asarray(element["center"], dtype=np.float64),
        "covariance": np.asarray(element["covariance"], dtype=np.float64),
        "rotation_matrix": np.asarray(element["rotation_matrix"], dtype=np.float64),
        "rotation_quaternion_wxyz": _quaternion_canonical(
            element["rotation_quaternion_wxyz"]
        ),
        "scale": np.asarray(element["scale"], dtype=np.float64),
        "opacity": float(element["opacity"]),
        "color": np.asarray(element["color"], dtype=np.float64),
        "curve_control_points": np.asarray(
            element["depth_curve"]["control_points"], dtype=np.float64
        ),
        "plane_normal": np.asarray(element["plane_normal"], dtype=np.float64),
        "stable_index": int(element["stable_index"]),
    }


def _semantic_digest(elements: list[dict[str, Any]]) -> str:
    normalized: list[Any] = []
    for element in sorted(elements, key=lambda item: item["id"]):
        semantic = _semantic_element(element)
        normalized.append(
            {
                key: (
                    np.round(value, 12).tolist()
                    if isinstance(value, np.ndarray)
                    else value
                )
                for key, value in semantic.items()
            }
        )
    return sha256_bytes(
        json.dumps(normalized, sort_keys=True).encode("utf-8")
    )


def _compare_normalized_element(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for field, baseline_value in _semantic_element(baseline).items():
        candidate_value = _semantic_element(candidate)[field]
        if isinstance(baseline_value, np.ndarray):
            error = float(np.max(np.abs(baseline_value - candidate_value)))
            if error > NORMALIZED_FLOAT_TOLERANCE:
                differences.append(
                    {
                        "scope": "normalized_element",
                        "element_id": baseline["id"],
                        "field": field,
                        "max_abs_error": error,
                        "tolerance": NORMALIZED_FLOAT_TOLERANCE,
                        "baseline_value": baseline_value.tolist(),
                        "candidate_value": candidate_value.tolist(),
                    }
                )
        elif isinstance(baseline_value, float):
            error = abs(float(baseline_value) - float(candidate_value))
            if error > NORMALIZED_FLOAT_TOLERANCE:
                differences.append(
                    {
                        "scope": "normalized_element",
                        "element_id": baseline["id"],
                        "field": field,
                        "max_abs_error": error,
                        "tolerance": NORMALIZED_FLOAT_TOLERANCE,
                        "baseline_value": baseline_value,
                        "candidate_value": candidate_value,
                    }
                )
        elif baseline_value != candidate_value:
            differences.append(
                {
                    "scope": "normalized_element",
                    "element_id": baseline["id"],
                    "field": field,
                    "max_abs_error": None,
                    "tolerance": NORMALIZED_FLOAT_TOLERANCE,
                    "baseline_value": baseline_value,
                    "candidate_value": candidate_value,
                }
            )
    return differences


def compare_normalized(project: GeneratedProject) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    documents = {
        version: read_json(project.root / "normalized" / f"{version}.json")
        for version in ("v1", "v2", "v3")
    }
    baseline = {element["id"]: element for element in documents["v1"]["elements"]}
    per_version_differences: dict[str, list[dict[str, Any]]] = {}
    version_summaries: dict[str, Any] = {}
    for version in ("v1", "v2", "v3"):
        differences: list[dict[str, Any]] = []
        elements = {element["id"]: element for element in documents[version]["elements"]}
        for element_id in sorted(set(baseline) - set(elements)):
            differences.append(
                {
                    "scope": "element_set",
                    "element_id": element_id,
                    "kind": "missing_in_candidate",
                    "baseline_version": "v1",
                    "candidate_version": version,
                }
            )
        for element_id in sorted(set(elements) - set(baseline)):
            differences.append(
                {
                    "scope": "element_set",
                    "element_id": element_id,
                    "kind": "extra_in_candidate",
                    "baseline_version": "v1",
                    "candidate_version": version,
                }
            )
        for element_id in sorted(set(baseline).intersection(elements)):
            for diff in _compare_normalized_element(
                baseline[element_id], elements[element_id]
            ):
                differences.append(
                    {
                        **diff,
                        "baseline_version": "v1",
                        "candidate_version": version,
                    }
                )
        per_version_differences[version] = differences
        version_summaries[version] = {
            "semantic_digest": _semantic_digest(documents[version]["elements"]),
            "element_count": len(documents[version]["elements"]),
            "issue_count": len(documents[version]["issues"]),
            "valid_normalized_difference_count": len(differences),
        }
    issues_by_version = {
        version: documents[version]["issues"] for version in ("v1", "v2", "v3")
    }
    result = {
        "schema_version": "normalization-difference-report/1",
        "scene_id": documents["v1"]["scene"]["id"],
        "baseline_version": "v1",
        "comparison_versions": ["v2", "v3"],
        "tolerance": {
            "float_abs": NORMALIZED_FLOAT_TOLERANCE,
            "quaternion": "canonical w>=0 before comparison",
        },
        "normalized_differences": [
            diff
            for version in ("v2", "v3")
            for diff in per_version_differences[version]
        ],
        "normalization_differences_by_version": {
            version: per_version_differences[version] for version in ("v2", "v3")
        },
        "schema_audit_issues": issues_by_version,
        "schema_audit_issue_summary": {
            version: {
                "count": len(issues_by_version[version]),
                "codes": sorted({issue["code"] for issue in issues_by_version[version]}),
                "element_ids": sorted(
                    {
                        str(issue["element_id"])
                        for issue in issues_by_version[version]
                        if issue["element_id"] is not None
                    }
                ),
            }
            for version in ("v1", "v2", "v3")
        },
        "version_summaries": version_summaries,
        "all_valid_elements_equivalent": all(
            not per_version_differences[version] for version in ("v2", "v3")
        ),
        "classification_rule": (
            "only differences between normalized element semantics are listed here; "
            "schema audit issues excluded by the normalizer are listed separately"
        ),
    }
    write_json_atomic(project.root / "normalized" / "normalization_diff.json", result)
    return result, per_version_differences


def _frame_data_path(project: GeneratedProject, version: str, record: dict[str, Any]) -> Path:
    return project.root / "frames" / version / record["data"]


def _load_version_frames(
    project: GeneratedProject, version: str
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[int, np.ndarray]]:
    frames_dir = project.root / "frames" / version
    checkpoint = read_json(frames_dir / "checkpoint.json")
    frame_json: dict[int, dict[str, Any]] = {}
    frame_records: dict[int, dict[str, Any]] = {}
    arrays: dict[int, np.ndarray] = {}
    for record in checkpoint["committed_frames"]:
        index = int(record["index"])
        frame_records[index] = record
        frame_json[index] = read_json(frames_dir / record["json"])
        arrays[index] = np.load(
            _frame_data_path(project, version, record), allow_pickle=False
        )
    return frame_json, frame_records, arrays


def _pair_signature(records: list[dict[str, Any]]) -> list[str]:
    return sorted(
        f"{item['element_a']}|{item['element_b']}" for item in records
    )


def compare_renders(
    project: GeneratedProject,
    normalized_differences: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    baseline_frames, _, baseline_arrays = _load_version_frames(project, "v1")
    version_results: dict[str, Any] = {}
    normalization_attributed: list[dict[str, Any]] = []
    ordering_differences: list[dict[str, Any]] = []
    sorting_strategy_differences: list[dict[str, Any]] = []
    render_differences: list[dict[str, Any]] = []
    validation_differences: list[dict[str, Any]] = []
    for version in ("v2", "v3"):
        frames, _, arrays = _load_version_frames(project, version)
        compared_indices = sorted(set(baseline_frames).intersection(frames))
        version_render_diffs: list[dict[str, Any]] = []
        for frame_index in compared_indices:
            rgba_a = baseline_arrays[frame_index]["rgba"]
            rgba_b = arrays[frame_index]["rgba"]
            delta = np.abs(rgba_a - rgba_b)
            max_delta = float(np.max(delta))
            mean_rgb_delta = float(np.mean(delta[..., :3]))
            changed = np.any(delta > PERCEPTUAL_CHANNEL_THRESHOLD, axis=2)
            changed_count = int(np.count_nonzero(changed))
            if changed_count:
                coordinates = np.argwhere(changed)
                y0, x0 = np.min(coordinates, axis=0)
                y1, x1 = np.max(coordinates, axis=0)
                changed_bbox = [int(x0), int(y0), int(x1), int(y1)]
            else:
                changed_bbox = None
            order_a = [item["id"] for item in baseline_frames[frame_index]["draw_order"]]
            order_b = [item["id"] for item in frames[frame_index]["draw_order"]]
            orders_match = order_a == order_b
            sensitive_a = _pair_signature(
                baseline_frames[frame_index]["sorting"]["sort_sensitive_pairs"]
            )
            sensitive_b = _pair_signature(
                frames[frame_index]["sorting"]["sort_sensitive_pairs"]
            )
            violations_a = _pair_signature(
                baseline_frames[frame_index]["sorting"]["violations"]
            )
            violations_b = _pair_signature(
                frames[frame_index]["sorting"]["violations"]
            )
            upstream_normalization = bool(normalized_differences[version])
            if orders_match:
                causal = []
            elif upstream_normalization:
                causal = ["normalization"]
            else:
                causal = ["sorting_strategy"]
            if not orders_match:
                entry = {
                    "candidate_version": version,
                    "frame_index": int(frame_index),
                    "baseline_order_near_to_far": order_a,
                    "candidate_order_near_to_far": order_b,
                    "causal_category": (
                        "normalization_upstream"
                        if upstream_normalization
                        else "sorting_strategy"
                    ),
                }
                ordering_differences.append(entry)
                if not upstream_normalization:
                    sorting_strategy_differences.append(entry)
            if sensitive_a != sensitive_b or violations_a != violations_b:
                validation_differences.append(
                    {
                        "candidate_version": version,
                        "frame_index": int(frame_index),
                        "sort_sensitive_pairs_match": sensitive_a == sensitive_b,
                        "violation_pairs_match": violations_a == violations_b,
                        "causal_category": (
                            "normalization_upstream"
                            if upstream_normalization
                            else "sorting_strategy"
                        ),
                    }
                )
            if max_delta > RENDER_FLOAT_TOLERANCE:
                if upstream_normalization:
                    classification = "normalization"
                elif not orders_match:
                    classification = "sorting_strategy"
                else:
                    classification = "numerical"
                record = {
                    "candidate_version": version,
                    "frame_index": int(frame_index),
                    "classification": classification,
                    "max_channel_difference": max_delta,
                    "mean_abs_rgb_difference": mean_rgb_delta,
                    "changed_pixel_count_over_1_255": changed_count,
                    "changed_bbox": changed_bbox,
                    "draw_order_changed": not orders_match,
                }
                render_differences.append(record)
                version_render_diffs.append(record)
                if classification == "normalization":
                    normalization_attributed.append(
                        {
                            "candidate_version": version,
                            "frame_index": int(frame_index),
                            "max_channel_difference": max_delta,
                            "source_normalization_difference_count": len(
                                normalized_differences[version]
                            ),
                        }
                    )
        version_results[version] = {
            "compared_frame_count": len(compared_indices),
            "render_difference_count": len(version_render_diffs),
            "draw_order_difference_count": sum(
                1
                for frame_index in compared_indices
                if [item["id"] for item in baseline_frames[frame_index]["draw_order"]]
                != [item["id"] for item in frames[frame_index]["draw_order"]]
            ),
            "max_channel_difference": max(
                (record["max_channel_difference"] for record in version_render_diffs),
                default=0.0,
            ),
        }
    result = {
        "schema_version": "render-comparison/1",
        "scene_fingerprint": project.fingerprint,
        "baseline_version": "v1",
        "comparison_versions": ["v2", "v3"],
        "tolerances": {
            "float_channel_difference": RENDER_FLOAT_TOLERANCE,
            "perceptual_changed_channel": PERCEPTUAL_CHANNEL_THRESHOLD,
        },
        "version_results": version_results,
        "render_differences": render_differences,
        "normalization_attributed_render_differences": normalization_attributed,
        "ordering_differences": ordering_differences,
        "sorting_strategy_differences": sorting_strategy_differences,
        "validation_differences": validation_differences,
        "all_frames_pixel_identical": not render_differences,
        "all_draw_orders_identical": not ordering_differences,
        "classification_rule": (
            "normalization differences, draw-order differences, and sorting-strategy "
            "differences are emitted in separate arrays; normalization wins as the "
            "upstream cause when both normalization and order differ"
        ),
    }
    write_json_atomic(project.root / "comparison.json", result)
    return result


def compare_all(project: GeneratedProject) -> dict[str, Any]:
    normalization, differences = compare_normalized(project)
    rendering = compare_renders(project, differences)
    summary = {
        "schema_version": "comparison-summary/1",
        "normalization_diff_file": str(
            project.root / "normalized" / "normalization_diff.json"
        ),
        "render_comparison_file": str(project.root / "comparison.json"),
        "all_valid_elements_equivalent": normalization[
            "all_valid_elements_equivalent"
        ],
        "all_frames_pixel_identical": rendering["all_frames_pixel_identical"],
        "all_draw_orders_identical": rendering["all_draw_orders_identical"],
        "normalization_difference_count": len(
            normalization["normalized_differences"]
        ),
        "render_difference_count": len(rendering["render_differences"]),
        "ordering_difference_count": len(rendering["ordering_differences"]),
        "sorting_strategy_difference_count": len(
            rendering["sorting_strategy_differences"]
        ),
    }
    write_json_atomic(project.root / "comparison_summary.json", summary)
    return summary
