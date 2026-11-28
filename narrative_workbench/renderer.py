from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .checkpoint import FrameCheckpoint
from .compositing import (
    composite_in_order,
    composite_layer_approximation,
    composite_pixel_sorted,
    sample_elements,
)
from .generator import GeneratedProject, generate_project
from .models import CameraFrame, GaussianElement
from .png import rgba_to_png_bytes
from .projection import project_frame
from .schemas import (
    NormalizationResult,
    load_camera_frames,
    load_normalized_scene,
)
from .storage import array_sha256, read_json, sha256_bytes, sha256_file, write_json_atomic
from .validation import analyze_order_and_coverage


def _reference_map(reference_frame: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    visible = {item["id"]: item for item in reference_frame.get("elements", [])}
    culled = {item["id"]: item for item in reference_frame.get("culled", [])}
    return visible, culled


def compare_projection_to_reference(
    projected: list[dict[str, Any]],
    culled: list[dict[str, Any]],
    reference_frame: dict[str, Any],
) -> dict[str, Any]:
    reference_visible, reference_culled = _reference_map(reference_frame)
    computed_visible = {record["id"]: record for record in projected}
    computed_culled = {record["id"]: record for record in culled}
    missing = sorted(set(reference_visible) - set(computed_visible))
    extra = sorted(set(computed_visible) - set(reference_visible))
    status_mismatches = sorted(
        set(reference_visible).intersection(computed_culled)
        | set(reference_culled).intersection(computed_visible)
    )
    center_error = 0.0
    covariance_error = 0.0
    axis_error = 0.0
    orientation_error = 0.0
    for element_id in sorted(set(reference_visible).intersection(computed_visible)):
        expected = reference_visible[element_id]
        actual = computed_visible[element_id]
        center_error = max(
            center_error,
            float(
                np.linalg.norm(
                    np.asarray(expected["screen_center"])
                    - np.asarray(actual["screen_center"])
                )
            ),
        )
        covariance_error = max(
            covariance_error,
            float(
                np.max(
                    np.abs(
                        np.asarray(expected["screen_covariance"])
                        - np.asarray(actual["screen_covariance"])
                    )
                )
            ),
        )
        axis_error = max(
            axis_error,
            abs(float(expected["semi_axis_major"]) - float(actual["semi_axis_major"])),
            abs(float(expected["semi_axis_minor"]) - float(actual["semi_axis_minor"])),
        )
        difference = abs(
            float(expected["orientation_deg"]) - float(actual["orientation_deg"])
        )
        orientation_error = max(orientation_error, min(difference, 180.0 - difference))
    passed = (
        not missing
        and not extra
        and not status_mismatches
        and center_error <= 1.0e-7
        and covariance_error <= 1.0e-7
        and axis_error <= 1.0e-7
        and orientation_error <= 1.0e-6
    )
    return {
        "passed": bool(passed),
        "reference": "scene/reference_ellipses.json",
        "missing_from_render": missing,
        "extra_in_render": extra,
        "status_mismatches": status_mismatches,
        "max_screen_center_error": center_error,
        "max_screen_covariance_error": covariance_error,
        "max_semi_axis_error": axis_error,
        "max_orientation_error_deg": orientation_error,
        "tolerance": {
            "center_px": 1.0e-7,
            "covariance_px2": 1.0e-7,
            "semi_axis_px": 1.0e-7,
            "orientation_deg": 1.0e-6,
        },
    }


def _draw_order(
    elements: list[GaussianElement],
    projected: list[dict[str, Any]],
) -> list[int]:
    by_id = {element.element_id: element for element in elements}
    stack_records = list(enumerate(projected))
    stack_records.sort(
        key=lambda pair: (
            -float(pair[1]["centroid_depth"]),
            int(by_id[pair[1]["id"]].layer_index),
            int(by_id[pair[1]["id"]].stable_index),
            by_id[pair[1]["id"]].element_id,
        )
    )
    return [index for index, _ in stack_records]


def _order_digest(
    draw_order: list[int],
    projected: list[dict[str, Any]],
) -> str:
    payload = "\n".join(
        f"{rank}:{projected[index]['id']}:{float(projected[index]['centroid_depth']):.12f}"
        for rank, index in enumerate(draw_order)
    )
    return sha256_bytes(payload.encode("utf-8"))


def _as_array_map(
    rgba_draw: np.ndarray,
    rgba_pixel_reference: np.ndarray,
    rgba_layer_approximation: np.ndarray,
    coverage_mask: np.ndarray,
    violation_mask: np.ndarray,
) -> dict[str, np.ndarray]:
    return {
        "rgba": rgba_draw.astype(np.float64, copy=False),
        "pixel_reference_rgba": rgba_pixel_reference.astype(np.float64, copy=False),
        "layer_approximation_rgba": rgba_layer_approximation.astype(
            np.float64, copy=False
        ),
        "coverage_mask": coverage_mask.astype(np.uint8, copy=False),
        "sorting_violation_mask": violation_mask.astype(np.uint8, copy=False),
    }


def render_one_frame(
    normalized: NormalizationResult,
    camera: CameraFrame,
    reference_frame: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, np.ndarray], bytes, str, str]:
    elements = normalized.scene.elements
    projected, culled = project_frame(elements, camera)
    reference_check = compare_projection_to_reference(
        projected, culled, reference_frame
    )
    draw_order = _draw_order(elements, projected)
    samples = sample_elements(elements, projected, camera)
    for stack_index, record in enumerate(projected):
        element = next(
            item for item in elements if item.element_id == record["id"]
        )
        normalized_depth = float(
            np.clip(
                (record["centroid_depth"] - camera.near)
                / max(camera.far - camera.near, 1.0e-12),
                0.0,
                1.0,
            )
        )
        gain = element.depth_curve.sample(normalized_depth)
        record["depth_modulation"] = {
            "normalized_depth": normalized_depth,
            "gain": float(gain),
            "base_opacity": float(element.opacity),
            "actual_opacity": float(samples.actual_opacities[stack_index]),
            "curve_samples": element.depth_curve.samples(),
        }
    rgba_draw = composite_in_order(draw_order, samples.alphas, samples.colors)
    rgba_pixel_reference = composite_pixel_sorted(
        list(range(samples.count)),
        samples.alphas,
        samples.depths,
        samples.colors,
    )
    rgba_layer_approximation, layer_composite_order = composite_layer_approximation(
        elements,
        list(range(samples.count)),
        samples,
        projected,
    )
    analysis = analyze_order_and_coverage(
        elements,
        projected,
        samples,
        draw_order,
        rgba_draw,
        rgba_pixel_reference,
        rgba_layer_approximation,
        camera,
    )
    analysis["degradation"]["layer_composite_order_far_to_near"] = [
        {
            "layer_index": int(layer_index),
            "layer_name": next(
                element.layer_name
                for element in elements
                if element.layer_index == layer_index
            ),
        }
        for layer_index in layer_composite_order
    ]
    draw_records: list[dict[str, Any]] = []
    for rank, stack_index in enumerate(draw_order):
        record = projected[stack_index]
        element = next(
            item for item in elements if item.element_id == record["id"]
        )
        draw_records.append(
            {
                "rank": rank,
                "id": element.element_id,
                "layer_index": int(element.layer_index),
                "layer_name": element.layer_name,
                "centroid_depth": float(record["centroid_depth"]),
                "actual_opacity": float(
                    record["depth_modulation"]["actual_opacity"]
                ),
                "stable_index": int(element.stable_index),
                "sort_key": [
                    -float(record["centroid_depth"]),
                    int(element.layer_index),
                    int(element.stable_index),
                    element.element_id,
                ],
            }
        )
    frame_payload = {
        "schema_version": "narrative-frame/1",
        "scene_id": normalized.scene.scene_id,
        "source_schema": normalized.source_schema,
        "frame_index": int(camera.frame_index),
        "camera": camera.to_dict(),
        "projection": {
            "visible": projected,
            "culled": culled,
            "reference_check": reference_check,
            "culling_policy": (
                "ellipsoid centroid and 3-sigma depth extent must stay strictly "
                "inside near/far; otherwise the element is excluded from blending"
            ),
        },
        "draw_order": draw_records,
        "sorting": {
            "strategy": "global_centroid_depth_far_to_near_with_stable_layer_source_ties",
            "sort_key": "(-centroid_depth, layer_index, stable_index, id)",
            "sort_sensitive_pairs": analysis["suspicious_pairs"],
            "violations": analysis["sorting_violations"],
        },
        "degradation": analysis["degradation"],
        "metrics": analysis["metrics"],
        "checks": analysis["checks"],
        "artifact_convention": {
            "alpha_convention": "straight alpha (non-premultiplied RGBA)",
            "rgba": "draw-order alpha-over composite",
            "pixel_reference_rgba": "per-pixel plane-depth sorted audit reference",
            "layer_approximation_rgba": (
                "per-pixel within each layer; layer groups ordered far-to-near "
                "by mean centroid depth"
            ),
            "sorting_violation_mask": "1 where a significant overlap pair reverses order",
            "range": "inclusive pixel coordinates",
        },
    }
    arrays = _as_array_map(
        rgba_draw,
        rgba_pixel_reference,
        rgba_layer_approximation,
        analysis["coverage_mask"],
        analysis["violation_mask"],
    )
    png_bytes = rgba_to_png_bytes(rgba_draw)
    rgba_hash = array_sha256(rgba_draw)
    order_digest = _order_digest(draw_order, projected)
    return frame_payload, arrays, png_bytes, rgba_hash, order_digest


def write_normalized_results(
    project: GeneratedProject,
    normalized_results: dict[str, NormalizationResult],
) -> dict[str, Any]:
    normalized_dir = project.root / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": "normalization-summary/1",
        "scene_id": project.normalized_v1.scene.scene_id,
        "versions": {},
    }
    all_issues: list[dict[str, Any]] = []
    for version in ("v1", "v2", "v3"):
        result = normalized_results[version]
        payload = result.to_dict()
        write_json_atomic(normalized_dir / f"{version}.json", payload)
        issues_payload = {
            "schema_version": "normalization-issues/1",
            "source_schema": version,
            "issues": [issue.to_dict() for issue in result.issues],
        }
        write_json_atomic(normalized_dir / f"issues_{version}.json", issues_payload)
        all_issues.extend(issue.to_dict() for issue in result.issues)
        summary["versions"][version] = {
            "element_count": len(result.scene.elements),
            "issue_count": len(result.issues),
            "valid": not any(issue.severity == "error" for issue in result.issues),
            "source_file": str(project.scene_files[version]),
            "normalized_file": str(normalized_dir / f"{version}.json"),
            "issue_codes": sorted({issue.code for issue in result.issues}),
            "element_ids": [element.element_id for element in result.scene.elements],
        }
    summary["total_issue_count"] = len(all_issues)
    write_json_atomic(normalized_dir / "issues_all.json", {
        "schema_version": "normalization-issues/1",
        "issues": all_issues,
    })
    write_json_atomic(normalized_dir / "summary.json", summary)
    return summary


def load_normalized_versions(
    project: GeneratedProject,
) -> tuple[list[CameraFrame], dict[str, NormalizationResult]]:
    camera_frames = load_camera_frames(project.camera_file)
    results = {
        version: load_normalized_scene(project.scene_files[version], camera_frames)
        for version in ("v1", "v2", "v3")
    }
    return camera_frames, results


def _reference_frames(project: GeneratedProject) -> dict[int, dict[str, Any]]:
    reference = read_json(project.reference_file)
    return {int(frame["frame"]): frame for frame in reference["frames"]}


def render_version(
    project: GeneratedProject,
    version: str,
    normalized: NormalizationResult,
    *,
    max_new_frames: int | None = None,
    reset_version: bool = False,
) -> dict[str, Any]:
    camera_fingerprint = sha256_file(project.camera_file)
    frames_dir = project.root / "frames" / version
    if reset_version and frames_dir.exists():
        for path in frames_dir.iterdir():
            if path.is_file() and (
                path.name == "checkpoint.json"
                or path.name.startswith("frame_")
                or path.name.startswith(".frame_")
            ):
                path.unlink()
    checkpoint = FrameCheckpoint(
        frames_dir,
        version=version,
        total_frames=len(normalized.scene.camera_frames),
        scene_fingerprint=project.fingerprint,
        camera_fingerprint=camera_fingerprint,
    )
    start_frame = checkpoint.session_start_frame
    references = _reference_frames(project)
    rendered_this_run: list[int] = []
    if not checkpoint.complete:
        stop_frame = checkpoint.total_frames
        if max_new_frames is not None and max_new_frames >= 0:
            stop_frame = min(stop_frame, checkpoint.next_frame + max_new_frames)
        for frame_index in range(checkpoint.next_frame, stop_frame):
            camera = normalized.scene.camera_frames[frame_index]
            payload, arrays, png_bytes, rgba_hash, order_digest = render_one_frame(
                normalized,
                camera,
                references[camera.frame_index],
            )
            checkpoint.commit(
                frame_index,
                frame_payload=payload,
                arrays=arrays,
                png_bytes=png_bytes,
                rgba_sha256=rgba_hash,
                order_digest=order_digest,
            )
            rendered_this_run.append(frame_index)
    summary = {
        **checkpoint.summary(),
        "source_schema": version,
        "rendered_frames_this_run": rendered_this_run,
        "committed_frames_skipped_this_run": list(range(start_frame)),
        "resume_policy": "start at persisted next_frame; no committed frame is replayed",
        "completed": checkpoint.complete,
    }
    write_json_atomic(frames_dir / "summary.json", summary)
    return summary


def render_all(
    output_dir: str | Path,
    *,
    seed: int,
    frame_count: int,
    width: int,
    height: int,
    versions: tuple[str, ...] = ("v1", "v2", "v3"),
    max_new_frames: int | None = None,
    reset_version: bool = False,
    force_generate: bool = False,
) -> dict[str, Any]:
    project = generate_project(
        output_dir,
        seed=seed,
        frame_count=frame_count,
        width=width,
        height=height,
        force=force_generate,
    )
    _, normalized_results = load_normalized_versions(project)
    write_normalized_results(project, normalized_results)
    version_summaries: dict[str, Any] = {}
    for version in versions:
        version_summaries[version] = render_version(
            project,
            version,
            normalized_results[version],
            max_new_frames=max_new_frames,
            reset_version=reset_version,
        )
    result = {
        "schema_version": "render-session/1",
        "scene_fingerprint": project.fingerprint,
        "versions": version_summaries,
        "scene_files": {key: str(value) for key, value in project.scene_files.items()},
        "camera_file": str(project.camera_file),
        "reference_file": str(project.reference_file),
    }
    write_json_atomic(project.root / "render_session.json", result)
    return result
