from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .geometry import distance
from .model import Furniture, Project, Vec3, ViewingPose
from .optics import (
    SceneHit,
    cast_scene_hit,
    compute_stereo_projection,
    pose_geometry,
    radial_scale_from_pose,
    screen_sample_point,
)
from .utils import canonical_float, percentile_nearest_rank

_MODEL_DESCRIPTION = {
    "parallax_sign": (
        "negative = crossed/convergent (in front of the zero-parallax plane); "
        "positive = uncrossed/divergent (behind the zero-parallax plane)"
    ),
    "aligned_fronto_parallel_formula": (
        "p_m = b * D * (1/Z0 - 1/Z); p_px = p_m / pixel_pitch_m"
    ),
    "reconstruction_formula": (
        "Z_reconstructed = 1 / (1/Z0 - p_m / (b_effective * D))"
    ),
    "sample_order": (
        "left-to-right, bottom-to-top; row 0 is the lower screen edge"
    ),
    "comfort_score_formula": (
        "100 * (1 - (w_ratio * overlimit_ratio + "
        "w_magnitude * mean_normalised_excess))"
    ),
}


@dataclass(frozen=True)
class _SampleResult:
    sub_row: int
    sub_col: int
    scene_hit: SceneHit
    raw_parallax_px: float
    display_parallax_px: float
    reconstructed_depth_m: float | None
    nominal_depth_m: float
    depth_distortion_ratio: float | None


def _rounded(value: float) -> float:
    return canonical_float(value, ndigits=9)


def _mean(values: Iterable[float]) -> float | None:
    materialized = list(values)
    if not materialized:
        return None
    return _rounded(sum(materialized) / len(materialized))


def _mask_row(values: list[bool]) -> str:
    return "".join("1" if value else "0" for value in values)


def evaluate_project(project: Project) -> dict[str, Any]:
    pose_summaries = [
        evaluate_pose(project, pose)
        for pose in sorted(project.viewing_poses, key=lambda item: item.id)
    ]
    return assemble_evaluation(project, pose_summaries)


def assemble_evaluation(
    project: Project,
    pose_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    pose_summaries = sorted(
        pose_summaries, key=lambda item: item["pose_id"]
    )
    layout = build_layout(project, pose_summaries)
    alignment = build_alignment(project, pose_summaries)
    total_blocks = sum(
        len(pose["blocks"]) for pose in pose_summaries
    )
    overlimit_blocks = sum(
        1
        for pose in pose_summaries
        for block in pose["blocks"]
        if block["overlimit"]
    )
    comfortable_blocks = total_blocks - overlimit_blocks
    mean_scores = [
        block["comfort_score"]
        for pose in pose_summaries
        for block in pose["blocks"]
    ]
    aggregate_mask_rows: list[str] = []
    for block_row in range(project.screen.blocks[1]):
        aggregate_mask_rows.append(
            "".join(
                "1"
                if any(
                    pose["overlimit_block_mask"][block_row][block_col] == "1"
                    for pose in pose_summaries
                )
                else "0"
                for block_col in range(project.screen.blocks[0])
            )
        )
    return {
        "schema_version": 1,
        "project_id": project.project_id,
        "project_fingerprint": project.fingerprint,
        "model": {
            "screen_pixel_pitch_m": _rounded(
                project.screen.pixel_pitch_m
            ),
            "screen_blocks": {
                "cols": project.screen.blocks[0],
                "rows": project.screen.blocks[1],
            },
            "samples_per_block": {
                "cols": project.screen.samples_per_block[0],
                "rows": project.screen.samples_per_block[1],
            },
            "sample_grid": {
                "cols": project.screen.sample_grid[0],
                "rows": project.screen.sample_grid[1],
            },
            **_MODEL_DESCRIPTION,
        },
        "configuration": {
            "interocular_distance_m": _rounded(
                project.stereo.interocular_distance_m
            ),
            "zero_parallax_distance_m": _rounded(
                project.screen.zero_parallax_distance_m
            ),
            "comfort_parallax_px": {
                "min": _rounded(
                    project.assessment.comfort_parallax_min_px
                ),
                "max": _rounded(
                    project.assessment.comfort_parallax_max_px
                ),
            },
            "depth_distortion_threshold": _rounded(
                project.assessment.depth_distortion_threshold
            ),
            "score_weights": {
                "overflow_ratio": _rounded(
                    project.assessment.overflow_ratio_weight
                ),
                "overflow_magnitude": _rounded(
                    project.assessment.overflow_magnitude_weight
                ),
            },
            "min_comfortable_block_ratio": _rounded(
                project.assessment.min_comfortable_block_ratio
            ),
            "viewing_zone_radius_m": _rounded(
                project.assessment.viewing_zone_radius_m
            ),
        },
        "summary": {
            "pose_count": len(pose_summaries),
            "block_count_per_pose": (
                project.screen.blocks[0] * project.screen.blocks[1]
            ),
            "total_block_count": total_blocks,
            "overlimit_block_count": overlimit_blocks,
            "comfortable_block_count": comfortable_blocks,
            "overlimit_block_ratio": (
                _rounded(overlimit_blocks / total_blocks)
                if total_blocks
                else 0.0
            ),
            "mean_comfort_score": (
                _rounded(sum(mean_scores) / len(mean_scores))
                if mean_scores
                else 0.0
            ),
        },
        "overlimit_block_mask": {
            "rows": aggregate_mask_rows,
            "description": (
                "1 if any viewing pose marks the block over limit"
            ),
        },
        "poses": pose_summaries,
        "layout": layout,
        "alignment": alignment,
    }


def evaluate_pose(
    project: Project,
    pose: ViewingPose,
    *,
    baseline_pose: dict[str, Any] | None = None,
    recompute_blocks: set[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    geometry = pose_geometry(project, pose)
    if geometry.screen_distance_m <= 0.0:
        raise ValueError(
            f"viewing pose '{pose.id}' is not in front of the screen"
        )
    block_cols, block_rows = project.screen.blocks
    all_indices = {
        (block_row, block_col)
        for block_row in range(block_rows)
        for block_col in range(block_cols)
    }
    selected = (
        all_indices
        if recompute_blocks is None
        else set(recompute_blocks)
    )
    selected &= all_indices
    old_blocks: dict[tuple[int, int], dict[str, Any]] = {}
    if baseline_pose is not None:
        old_blocks = {
            (block["row"], block["col"]): block
            for block in baseline_pose.get("blocks", [])
        }

    blocks: list[dict[str, Any]] = []
    for block_row in range(block_rows):
        for block_col in range(block_cols):
            index = (block_row, block_col)
            if index in selected or index not in old_blocks:
                block = evaluate_block(
                    project,
                    pose,
                    geometry,
                    block_row,
                    block_col,
                )
            else:
                block = old_blocks[index]
            blocks.append(block)
    return summarize_pose(project, pose, geometry, blocks)


def evaluate_block(
    project: Project,
    pose: ViewingPose,
    geometry: Any,
    block_row: int,
    block_col: int,
) -> dict[str, Any]:
    samples_per_block_cols, samples_per_block_rows = (
        project.screen.samples_per_block
    )
    sample_results: list[_SampleResult] = []
    for sub_row in range(samples_per_block_rows):
        sample_row = (
            block_row * samples_per_block_rows + sub_row
        )
        for sub_col in range(samples_per_block_cols):
            sample_col = (
                block_col * samples_per_block_cols + sub_col
            )
            target = screen_sample_point(
                project.screen, sample_row, sample_col
            )
            hit = cast_scene_hit(project, pose, target)
            projection = compute_stereo_projection(
                project, pose, hit, geometry
            )
            depth_error = None
            if projection.reconstructed_depth_m is not None:
                depth_error = abs(
                    projection.reconstructed_depth_m
                    - projection.nominal_depth_m
                ) / max(projection.nominal_depth_m, 1e-12)
            sample_results.append(
                _SampleResult(
                    sub_row=sub_row,
                    sub_col=sub_col,
                    scene_hit=hit,
                    raw_parallax_px=projection.raw_parallax_px,
                    display_parallax_px=projection.display_parallax_px,
                    reconstructed_depth_m=(
                        projection.reconstructed_depth_m
                    ),
                    nominal_depth_m=projection.nominal_depth_m,
                    depth_distortion_ratio=depth_error,
                )
            )

    lower = project.assessment.comfort_parallax_min_px
    upper = project.assessment.comfort_parallax_max_px
    span = upper - lower
    threshold = project.assessment.depth_distortion_threshold
    parallax_values = [sample.raw_parallax_px for sample in sample_results]
    parallax_mask = [
        value < lower or value > upper for value in parallax_values
    ]
    depth_mask = [
        sample.depth_distortion_ratio is None
        or sample.depth_distortion_ratio > threshold
        for sample in sample_results
    ]
    combined_mask = [
        parallax_mask[index] or depth_mask[index]
        for index in range(len(sample_results))
    ]
    below_excess = [max(lower - value, 0.0) for value in parallax_values]
    above_excess = [max(value - upper, 0.0) for value in parallax_values]
    below_count = sum(
        value < lower for value in parallax_values
    )
    above_count = sum(
        value > upper for value in parallax_values
    )
    normalized_excess = [
        min(1.0, max(below_excess[index], above_excess[index]) / span)
        for index in range(len(sample_results))
    ]
    depth_normalized_excess: list[float] = []
    for sample in sample_results:
        if sample.depth_distortion_ratio is None:
            depth_normalized_excess.append(1.0)
        elif threshold <= 0.0:
            depth_normalized_excess.append(
                1.0 if sample.depth_distortion_ratio > 0.0 else 0.0
            )
        else:
            depth_normalized_excess.append(
                min(
                    1.0,
                    max(
                        0.0,
                        sample.depth_distortion_ratio - threshold,
                    )
                    / threshold,
                )
            )
    combined_normalized_excess = [
        max(
            normalized_excess[index],
            depth_normalized_excess[index],
        )
        for index in range(len(sample_results))
    ]
    parallax_overlimit_ratio = sum(parallax_mask) / len(parallax_mask)
    overlimit_ratio = sum(combined_mask) / len(combined_mask)
    mean_normalized_excess = sum(combined_normalized_excess) / len(
        combined_normalized_excess
    )
    comfort_score = 100.0 * (
        1.0
        - (
            project.assessment.overflow_ratio_weight * overlimit_ratio
            + project.assessment.overflow_magnitude_weight
            * mean_normalized_excess
        )
    )
    comfort_score = max(0.0, min(100.0, comfort_score))
    cause_details = _primary_cause(
        below_count,
        above_count,
        sum(below_excess),
        sum(above_excess),
    )

    valid_depth_samples = [
        sample
        for sample in sample_results
        if sample.reconstructed_depth_m is not None
    ]
    depth_ratios = [
        sample.depth_distortion_ratio
        for sample in valid_depth_samples
        if sample.depth_distortion_ratio is not None
    ]
    depth_scale_ratios = [
        sample.reconstructed_depth_m / sample.nominal_depth_m
        for sample in valid_depth_samples
        if sample.reconstructed_depth_m is not None
        and sample.nominal_depth_m > 0.0
    ]
    furniture_depths: dict[str, dict[str, Any]] = {}
    for sample in valid_depth_samples:
        object_id = sample.scene_hit.object_id
        scale_value = (
            sample.reconstructed_depth_m / sample.nominal_depth_m
            if sample.reconstructed_depth_m is not None
            and sample.nominal_depth_m > 0.0
            else None
        )
        entry = furniture_depths.setdefault(
            object_id,
            {
                "sample_count": 0,
                "depth_scale_sum": 0.0,
                "depth_scale_min": None,
                "depth_scale_max": None,
            },
        )
        entry["sample_count"] += 1
        if scale_value is not None and math.isfinite(scale_value):
            entry["depth_scale_sum"] += scale_value
            if (
                entry["depth_scale_min"] is None
                or scale_value < entry["depth_scale_min"]
            ):
                entry["depth_scale_min"] = scale_value
            if (
                entry["depth_scale_max"] is None
                or scale_value > entry["depth_scale_max"]
            ):
                entry["depth_scale_max"] = scale_value
        entry["depth_scale_mean"] = (
            entry["depth_scale_sum"] / entry["sample_count"]
            if entry["sample_count"]
            else None
        )
    for entry in furniture_depths.values():
        entry["depth_scale_sum"] = _rounded(entry["depth_scale_sum"])
        if entry["depth_scale_mean"] is not None:
            entry["depth_scale_mean"] = _rounded(
                entry["depth_scale_mean"]
            )
        if entry["depth_scale_min"] is not None:
            entry["depth_scale_min"] = _rounded(
                entry["depth_scale_min"]
            )
        if entry["depth_scale_max"] is not None:
            entry["depth_scale_max"] = _rounded(
                entry["depth_scale_max"]
            )

    row_major_index = 0
    parallax_mask_rows: list[str] = []
    depth_mask_rows: list[str] = []
    combined_mask_rows: list[str] = []
    for _ in range(samples_per_block_rows):
        parallax_slice = parallax_mask[
            row_major_index : row_major_index + samples_per_block_cols
        ]
        depth_slice = depth_mask[
            row_major_index : row_major_index + samples_per_block_cols
        ]
        combined_slice = combined_mask[
            row_major_index : row_major_index + samples_per_block_cols
        ]
        parallax_mask_rows.append(_mask_row(parallax_slice))
        depth_mask_rows.append(_mask_row(depth_slice))
        combined_mask_rows.append(_mask_row(combined_slice))
        row_major_index += samples_per_block_cols

    valid_reconstructed = [
        sample.reconstructed_depth_m
        for sample in valid_depth_samples
        if sample.reconstructed_depth_m is not None
    ]
    nominal_depths = [sample.nominal_depth_m for sample in sample_results]
    limit_causes: list[str] = []
    if below_count:
        limit_causes.append("parallax_convergence")
    if above_count:
        limit_causes.append("parallax_divergence")
    if sum(depth_mask):
        limit_causes.append("depth_distortion")
    return {
        "block_id": f"r{block_row}c{block_col}",
        "row": block_row,
        "col": block_col,
        "pixel_bounds": {
            "col_start": (
                block_col
                * project.screen.resolution[0]
                // project.screen.blocks[0]
            ),
            "col_end_exclusive": (
                (block_col + 1)
                * project.screen.resolution[0]
                // project.screen.blocks[0]
            ),
            "row_start": (
                block_row
                * project.screen.resolution[1]
                // project.screen.blocks[1]
            ),
            "row_end_exclusive": (
                (block_row + 1)
                * project.screen.resolution[1]
                // project.screen.blocks[1]
            ),
        },
        "sample_count": len(sample_results),
        "parallax_statistics": {
            "min_px": _rounded(min(parallax_values)),
            "max_px": _rounded(max(parallax_values)),
            "mean_px": _rounded(
                sum(parallax_values) / len(parallax_values)
            ),
            "p95_abs_px": _rounded(
                percentile_nearest_rank(
                    [abs(value) for value in parallax_values], 0.95
                )
            ),
            "display_min_px": _rounded(
                min(
                    sample.display_parallax_px
                    for sample in sample_results
                )
            ),
            "display_max_px": _rounded(
                max(
                    sample.display_parallax_px
                    for sample in sample_results
                )
            ),
            "below_comfort_count": below_count,
            "above_comfort_count": above_count,
        },
        "depth_statistics": {
            "nominal_min_m": _rounded(min(nominal_depths)),
            "nominal_max_m": _rounded(max(nominal_depths)),
            "nominal_mean_m": _rounded(
                sum(nominal_depths) / len(nominal_depths)
            ),
            "reconstructed_min_m": (
                _rounded(min(valid_reconstructed))
                if valid_reconstructed
                else None
            ),
            "reconstructed_max_m": (
                _rounded(max(valid_reconstructed))
                if valid_reconstructed
                else None
            ),
            "reconstructed_mean_m": (
                _rounded(
                    sum(valid_reconstructed)
                    / len(valid_reconstructed)
                )
                if valid_reconstructed
                else None
            ),
            "distortion_ratio_mean": (
                _rounded(sum(depth_ratios) / len(depth_ratios))
                if depth_ratios
                else None
            ),
            "distortion_ratio_max": (
                _rounded(max(depth_ratios))
                if depth_ratios
                else None
            ),
            "invalid_reconstruction_count": (
                len(sample_results) - len(valid_depth_samples)
            ),
        },
        "depth_scale": {
            "sample_count": len(depth_scale_ratios),
            "sum": _rounded(sum(depth_scale_ratios)),
            "min": (
                _rounded(min(depth_scale_ratios))
                if depth_scale_ratios
                else None
            ),
            "max": (
                _rounded(max(depth_scale_ratios))
                if depth_scale_ratios
                else None
            ),
            "mean": (
                _rounded(
                    sum(depth_scale_ratios)
                    / len(depth_scale_ratios)
                )
                if depth_scale_ratios
                else None
            ),
        },
        "furniture_depths": furniture_depths,
        "hit_sample_counts": {
            object_id: sum(
                sample.scene_hit.object_id == object_id
                for sample in sample_results
            )
            for object_id in sorted(
                {sample.scene_hit.object_id for sample in sample_results}
            )
        },
        "parallax_overlimit_mask": parallax_mask_rows,
        "depth_overlimit_mask": depth_mask_rows,
        "overlimit_mask": combined_mask_rows,
        "parallax_overlimit": any(parallax_mask),
        "depth_overlimit": any(depth_mask),
        "overlimit": any(combined_mask),
        "depth_only_overlimit": (
            any(combined_mask) and not any(parallax_mask)
        ),
        "overlimit_sample_count": sum(combined_mask),
        "overlimit_sample_ratio": _rounded(
            sum(combined_mask) / len(combined_mask)
        ),
        "parallax_overlimit_sample_ratio": _rounded(
            parallax_overlimit_ratio
        ),
        "depth_overlimit_sample_ratio": _rounded(
            sum(depth_mask) / len(depth_mask)
        ),
        "comfort_score": _rounded(comfort_score),
        "primary_cause": cause_details["category"],
        "cause_details": cause_details,
        "limit_causes": limit_causes,
    }


def _primary_cause(
    below_count: int,
    above_count: int,
    below_pressure: float,
    above_pressure: float,
) -> dict[str, Any]:
    if below_count == 0 and above_count == 0:
        category = "within_comfort"
    elif below_count and above_count:
        smaller = min(below_pressure, above_pressure)
        larger = max(below_pressure, above_pressure)
        if smaller <= 0.0 or larger / max(smaller, 1e-12) >= 1.5:
            category = (
                "convergence_overflow"
                if below_pressure >= above_pressure
                else "divergence_overflow"
            )
        else:
            category = "mixed_overflow"
    elif below_count:
        category = "convergence_overflow"
    else:
        category = "divergence_overflow"
    return {
        "category": category,
        "convergence_sample_count": below_count,
        "divergence_sample_count": above_count,
        "convergence_excess_px": _rounded(below_pressure),
        "divergence_excess_px": _rounded(above_pressure),
    }


def summarize_pose(
    project: Project,
    pose: ViewingPose,
    geometry: Any,
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    block_cols, block_rows = project.screen.blocks
    block_by_index = {
        (block["row"], block["col"]): block for block in blocks
    }
    overlimit_block_mask: list[str] = []
    comfort_score_rows: list[list[float]] = []
    for row in range(block_rows):
        mask: list[str] = []
        score_row: list[float] = []
        for col in range(block_cols):
            block = block_by_index[(row, col)]
            mask.append("1" if block["overlimit"] else "0")
            score_row.append(block["comfort_score"])
        overlimit_block_mask.append("".join(mask))
        comfort_score_rows.append(score_row)

    comfortable = [
        block for block in blocks if not block["overlimit"]
    ]
    overlimit = [
        block for block in blocks if block["overlimit"]
    ]
    comfortable_ratio = (
        len(comfortable) / len(blocks) if blocks else 0.0
    )
    visual_eligible = bool(comfortable) and (
        comfortable_ratio
        >= project.assessment.min_comfortable_block_ratio
    )
    visual_radius = 0.0
    if visual_eligible:
        visual_radius = project.assessment.viewing_zone_radius_m * (
            0.5 + 0.5 * comfortable_ratio
        )

    scale_count = sum(
        block["depth_scale"]["sample_count"] for block in blocks
    )
    scale_sum = sum(block["depth_scale"]["sum"] for block in blocks)
    observed_scales = [
        block["depth_scale"]["mean"]
        for block in blocks
        if block["depth_scale"]["mean"] is not None
    ]
    furniture_entries: dict[str, dict[str, Any]] = {}
    for block in blocks:
        for object_id, value in block["furniture_depths"].items():
            entry = furniture_entries.setdefault(
                object_id,
                {
                    "sample_count": 0,
                    "depth_scale_sum": 0.0,
                    "depth_scale_min": None,
                    "depth_scale_max": None,
                },
            )
            entry["sample_count"] += value["sample_count"]
            entry["depth_scale_sum"] += value["depth_scale_sum"]
            if value["depth_scale_min"] is not None:
                entry["depth_scale_min"] = (
                    value["depth_scale_min"]
                    if entry["depth_scale_min"] is None
                    else min(entry["depth_scale_min"], value["depth_scale_min"])
                )
            if value["depth_scale_max"] is not None:
                entry["depth_scale_max"] = (
                    value["depth_scale_max"]
                    if entry["depth_scale_max"] is None
                    else max(entry["depth_scale_max"], value["depth_scale_max"])
                )
    for entry in furniture_entries.values():
        entry["depth_scale_sum"] = _rounded(entry["depth_scale_sum"])
        entry["depth_scale_mean"] = (
            _rounded(entry["depth_scale_sum"] / entry["sample_count"])
            if entry["sample_count"]
            else None
        )
        for key in ("depth_scale_min", "depth_scale_max"):
            if entry[key] is not None:
                entry[key] = _rounded(entry[key])

    hit_counts: dict[str, int] = {}
    for block in blocks:
        for object_id, count in block["hit_sample_counts"].items():
            hit_counts[object_id] = hit_counts.get(object_id, 0) + count

    return {
        "pose_id": pose.id,
        "position_m": [_rounded(value) for value in pose.position],
        "forward": [_rounded(value) for value in pose.forward],
        "furniture_id": pose.furniture_id,
        "screen_distance_m": _rounded(geometry.screen_distance_m),
        "block_count": len(blocks),
        "comfortable_block_count": len(comfortable),
        "overlimit_block_count": len(overlimit),
        "comfortable_block_ratio": _rounded(comfortable_ratio),
        "mean_comfort_score": (
            _rounded(
                sum(block["comfort_score"] for block in blocks)
                / len(blocks)
            )
            if blocks
            else 0.0
        ),
        "overlimit_block_mask": overlimit_block_mask,
        "comfort_score_grid": [
            [_rounded(value) for value in score_row]
            for score_row in comfort_score_rows
        ],
        "depth_scale_statistics": {
            "sample_count": scale_count,
            "sum": _rounded(scale_sum),
            "mean": (
                _rounded(scale_sum / scale_count)
                if scale_count
                else None
            ),
            "min": (
                _rounded(min(observed_scales))
                if observed_scales
                else None
            ),
            "max": (
                _rounded(max(observed_scales))
                if observed_scales
                else None
            ),
        },
        "furniture_depth_scales": furniture_entries,
        "hit_sample_counts": {
            key: hit_counts[key] for key in sorted(hit_counts)
        },
        "visual_region": {
            "eligible": visual_eligible,
            "center_m": [
                _rounded(pose.position[0]),
                _rounded(pose.position[1]),
            ],
            "radius_m": _rounded(visual_radius),
            "rule": (
                "radius = viewing_zone_radius_m * "
                "(0.5 + 0.5 * comfortable_block_ratio) when the "
                "minimum comfortable block ratio is met"
            ),
        },
        "blocks": blocks,
    }


def _screen_anchor(project: Project, reference: str) -> Vec3 | None:
    if reference == f"screen:{project.screen.id}":
        return project.screen.center
    return None


def _anchor_for_reference(
    project: Project,
    furniture_by_id: dict[str, Furniture],
    reference: str,
) -> Vec3:
    screen_anchor = _screen_anchor(project, reference)
    if screen_anchor is not None:
        return screen_anchor
    return furniture_by_id[reference].center


def _endpoint_scale(
    project: Project,
    pose_summary: dict[str, Any],
    reference: str,
) -> tuple[float, str]:
    if reference.startswith("screen:"):
        screen_distance = pose_summary["screen_distance_m"]
        if screen_distance <= 0.0:
            return 1.0, "unavailable"
        return (
            project.screen.zero_parallax_distance_m / screen_distance,
            "zero_parallax_mapping",
        )
    item = pose_summary["furniture_depth_scales"].get(reference)
    if item is not None and item.get("depth_scale_mean") is not None:
        return float(item["depth_scale_mean"]), "observed_screen_samples"
    overall = pose_summary["depth_scale_statistics"]["mean"]
    if overall is not None:
        return float(overall), "pose_depth_scale_fallback"
    return 1.0, "unity_fallback"


def build_layout(
    project: Project,
    pose_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    furniture_by_id = project.furniture_by_id()
    relationships: list[dict[str, Any]] = []
    threshold = project.assessment.depth_distortion_threshold
    for relationship in sorted(
        project.relationships, key=lambda item: item.id
    ):
        from_anchor = _anchor_for_reference(
            project, furniture_by_id, relationship.from_ref
        )
        to_anchor = _anchor_for_reference(
            project, furniture_by_id, relationship.to_ref
        )
        nominal_distance = distance(from_anchor, to_anchor)
        pose_results: list[dict[str, Any]] = []
        for pose_summary in pose_summaries:
            pose_position = tuple(pose_summary["position_m"])
            from_scale, from_source = _endpoint_scale(
                project, pose_summary, relationship.from_ref
            )
            to_scale, to_source = _endpoint_scale(
                project, pose_summary, relationship.to_ref
            )
            adjusted_from = radial_scale_from_pose(
                pose_position, from_anchor, from_scale
            )
            adjusted_to = radial_scale_from_pose(
                pose_position, to_anchor, to_scale
            )
            perceptual_distance = distance(adjusted_from, adjusted_to)
            delta = perceptual_distance - nominal_distance
            relative_delta = (
                abs(delta) / nominal_distance
                if nominal_distance > 0.0
                else 0.0
            )
            contributor_entries: list[dict[str, Any]] = []
            endpoint_refs = {relationship.from_ref, relationship.to_ref}
            for block in pose_summary["blocks"]:
                relevant_samples = 0
                relevant_scale_sum = 0.0
                for reference in endpoint_refs:
                    if reference.startswith("screen:"):
                        block_scale = pose_summary[
                            "depth_scale_statistics"
                        ]["mean"]
                        if block_scale is None:
                            block_scale = 1.0
                        count = block["sample_count"]
                        relevant_samples += count
                        relevant_scale_sum += float(block_scale) * count
                    else:
                        item = block["furniture_depths"].get(reference)
                        if item is not None and item.get(
                            "depth_scale_mean"
                        ) is not None:
                            count = item["sample_count"]
                            relevant_samples += count
                            relevant_scale_sum += (
                                float(item["depth_scale_mean"]) * count
                            )
                if relevant_samples <= 0:
                    continue
                block_scale = relevant_scale_sum / relevant_samples
                contributor_entries.append(
                    {
                        "block_id": block["block_id"],
                        "row": block["row"],
                        "col": block["col"],
                        "sample_count": relevant_samples,
                        "block_depth_scale": _rounded(block_scale),
                        "scale_delta": _rounded(block_scale - 1.0),
                        "parallax_mean_px": block[
                            "parallax_statistics"
                        ]["mean_px"],
                        "overlimit": block["overlimit"],
                    }
                )
            total_samples = sum(
                entry["sample_count"] for entry in contributor_entries
            )
            for entry in contributor_entries:
                entry["share"] = (
                    _rounded(entry["sample_count"] / total_samples)
                    if total_samples
                    else 0.0
                )
            contributor_entries.sort(
                key=lambda entry: (
                    -abs(entry["scale_delta"]),
                    entry["row"],
                    entry["col"],
                )
            )
            pose_results.append(
                {
                    "pose_id": pose_summary["pose_id"],
                    "from_scale": _rounded(from_scale),
                    "from_scale_source": from_source,
                    "to_scale": _rounded(to_scale),
                    "to_scale_source": to_source,
                    "nominal_distance_m": _rounded(nominal_distance),
                    "perceptual_distance_m": _rounded(perceptual_distance),
                    "delta_m": _rounded(delta),
                    "relative_delta": _rounded(relative_delta),
                    "distorted": relative_delta > threshold,
                    "contributors": contributor_entries,
                }
            )
        worst = max(
            pose_results,
            key=lambda entry: (
                abs(entry["delta_m"]),
                entry["pose_id"],
            ),
        )
        distortion_regions = []
        for pose_result in pose_results:
            if not pose_result["distorted"]:
                continue
            contributing_blocks = [
                {
                    "block_id": entry["block_id"],
                    "row": entry["row"],
                    "col": entry["col"],
                    "share": entry["share"],
                    "scale_delta": entry["scale_delta"],
                }
                for entry in pose_result["contributors"]
                if abs(entry["scale_delta"]) > threshold
            ]
            if not contributing_blocks:
                contributing_blocks = [
                    {
                        "block_id": entry["block_id"],
                        "row": entry["row"],
                        "col": entry["col"],
                        "share": entry["share"],
                        "scale_delta": entry["scale_delta"],
                    }
                    for entry in pose_result["contributors"][:3]
                ]
            distortion_regions.append(
                {
                    "pose_id": pose_result["pose_id"],
                    "relative_delta": pose_result["relative_delta"],
                    "blocks": contributing_blocks,
                }
            )
        relationships.append(
            {
                "relationship_id": relationship.id,
                "from": relationship.from_ref,
                "to": relationship.to_ref,
                "nominal_distance_m": _rounded(nominal_distance),
                "distorted": any(
                    result["distorted"] for result in pose_results
                ),
                "max_abs_delta_m": _rounded(
                    max(abs(result["delta_m"]) for result in pose_results)
                ),
                "max_abs_relative_delta": _rounded(
                    max(
                        result["relative_delta"]
                        for result in pose_results
                    )
                ),
                "worst_pose_id": worst["pose_id"],
                "poses": pose_results,
                "distortion_regions": distortion_regions,
            }
        )
    furniture_proportions: list[dict[str, Any]] = []
    for pose_summary in pose_summaries:
        pose_position = tuple(pose_summary["position_m"])
        for furniture in sorted(project.furniture, key=lambda item: item.id):
            scale, scale_source = _endpoint_scale(
                project, pose_summary, furniture.id
            )
            adjusted_corners = [
                radial_scale_from_pose(
                    pose_position, corner, scale
                )
                for corner in furniture.corners
            ]
            nominal_extents = [
                furniture.maximum[axis] - furniture.minimum[axis]
                for axis in range(3)
            ]
            perceptual_extents = [
                max(corner[axis] for corner in adjusted_corners)
                - min(corner[axis] for corner in adjusted_corners)
                for axis in range(3)
            ]
            extent_ratios = [
                perceptual_extents[axis] / nominal_extents[axis]
                for axis in range(3)
            ]
            relative_extent_deltas = [
                abs(perceptual_extents[axis] - nominal_extents[axis])
                / nominal_extents[axis]
                for axis in range(3)
            ]
            contributors = []
            for block in pose_summary["blocks"]:
                item = block["furniture_depths"].get(furniture.id)
                if item is None or item.get("depth_scale_mean") is None:
                    continue
                contributors.append(
                    {
                        "block_id": block["block_id"],
                        "row": block["row"],
                        "col": block["col"],
                        "sample_count": item["sample_count"],
                        "block_depth_scale": item["depth_scale_mean"],
                    }
                )
            total_contributor_samples = sum(
                entry["sample_count"] for entry in contributors
            )
            for entry in contributors:
                entry["share"] = (
                    _rounded(
                        entry["sample_count"]
                        / total_contributor_samples
                    )
                    if total_contributor_samples
                    else 0.0
                )
            contributors.sort(
                key=lambda entry: (
                    -abs(entry["block_depth_scale"] - 1.0),
                    entry["row"],
                    entry["col"],
                )
            )
            furniture_proportions.append(
                {
                    "furniture_id": furniture.id,
                    "pose_id": pose_summary["pose_id"],
                    "depth_scale": _rounded(scale),
                    "depth_scale_source": scale_source,
                    "nominal_extent_m": [
                        _rounded(value) for value in nominal_extents
                    ],
                    "perceptual_extent_m": [
                        _rounded(value) for value in perceptual_extents
                    ],
                    "extent_ratio": [
                        _rounded(value) for value in extent_ratios
                    ],
                    "max_relative_extent_delta": _rounded(
                        max(relative_extent_deltas)
                    ),
                    "distorted": (
                        max(relative_extent_deltas) > threshold
                    ),
                    "contributing_blocks": contributors,
                }
            )
    return {
        "threshold": _rounded(threshold),
        "furniture_proportions": furniture_proportions,
        "relationships": relationships,
    }


def _point_in_discs(
    point: tuple[float, float],
    discs: list[tuple[str, tuple[float, float], float]],
) -> bool:
    for _, center, radius in discs:
        if math.hypot(
            point[0] - center[0], point[1] - center[1]
        ) <= radius + 1e-12:
            return True
    return False


def _distance_to_disc_union(
    point: tuple[float, float],
    discs: list[tuple[str, tuple[float, float], float]],
    fallback: float = 0.0,
) -> float:
    if not discs:
        return fallback
    gaps = [
        max(
            0.0,
            math.hypot(
                point[0] - center[0], point[1] - center[1]
            )
            - radius,
        )
        for _, center, radius in discs
    ]
    return min(gaps)


def build_alignment(
    project: Project,
    pose_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    visual_discs = [
        (
            pose["pose_id"],
            (
                float(pose["visual_region"]["center_m"][0]),
                float(pose["visual_region"]["center_m"][1]),
            ),
            float(pose["visual_region"]["radius_m"]),
        )
        for pose in pose_summaries
        if pose["visual_region"]["eligible"]
        and float(pose["visual_region"]["radius_m"]) > 0.0
    ]
    acoustic_discs = [
        (
            sample.id,
            sample.position,
            sample.coverage_radius_m,
        )
        for sample in sorted(
            project.acoustic_samples, key=lambda item: item.id
        )
    ]
    visual_gap_fallback = math.hypot(
        project.room.width_m, project.room.depth_m
    )

    probe_specs: list[tuple[str, str, tuple[float, float]]] = []
    directions = (
        ("center", 0.0, 0.0),
        ("east", 1.0, 0.0),
        ("northeast", 1.0, 1.0),
        ("north", 0.0, 1.0),
        ("northwest", -1.0, 1.0),
        ("west", -1.0, 0.0),
        ("southwest", -1.0, -1.0),
        ("south", 0.0, -1.0),
        ("southeast", 1.0, -1.0),
    )
    for pose in pose_summaries:
        region = pose["visual_region"]
        if not region["eligible"]:
            continue
        center_x = float(region["center_m"][0])
        center_y = float(region["center_m"][1])
        radius = float(region["radius_m"])
        for name, direction_x, direction_y in directions:
            length = math.hypot(direction_x, direction_y)
            if length == 0.0:
                factor = 0.0
            else:
                factor = 0.6 * radius / length
            point = (
                center_x + direction_x * factor,
                center_y + direction_y * factor,
            )
            probe_specs.append(
                (
                    f"visual:{pose['pose_id']}:{name}",
                    pose["pose_id"],
                    point,
                )
            )

    mismatches: list[dict[str, Any]] = []
    for probe_id, pose_id, point in probe_specs:
        if _point_in_discs(point, acoustic_discs):
            continue
        gap = _distance_to_disc_union(point, acoustic_discs)
        mismatches.append(
            {
                "sample_id": probe_id,
                "pose_id": pose_id,
                "type": "visual_only",
                "description": "visual region is reachable but acoustic coverage is absent",
                "position_m": [_rounded(point[0]), _rounded(point[1])],
                "distance_m": _rounded(gap),
            }
        )

    acoustic_details: list[dict[str, Any]] = []
    for sample in sorted(
        project.acoustic_samples, key=lambda item: item.id
    ):
        point = (
            float(sample.position[0]),
            float(sample.position[1]),
        )
        visual_reachable = _point_in_discs(point, visual_discs)
        acoustic_covered = _point_in_discs(point, acoustic_discs)
        acoustic_details.append(
            {
                "sample_id": sample.id,
                "position_m": [
                    _rounded(point[0]),
                    _rounded(point[1]),
                ],
                "coverage_radius_m": _rounded(
                    sample.coverage_radius_m
                ),
                "visual_reachable": visual_reachable,
                "acoustic_covered": acoustic_covered,
                "viewing_pose_id": sample.viewing_pose_id,
            }
        )
        if not visual_reachable:
            gap = _distance_to_disc_union(
                point,
                visual_discs,
                fallback=visual_gap_fallback,
            )
            mismatches.append(
                {
                    "sample_id": sample.id,
                    "pose_id": sample.viewing_pose_id,
                    "type": "acoustic_only",
                    "description": (
                        "acoustic coverage is present but visual region is "
                        "not reachable"
                    ),
                    "position_m": [
                        _rounded(point[0]),
                        _rounded(point[1]),
                    ],
                    "distance_m": _rounded(gap),
                }
            )

    mismatches.sort(
        key=lambda item: (
            item["type"],
            item["sample_id"],
        )
    )
    counts = {
        "visual_only": sum(
            item["type"] == "visual_only" for item in mismatches
        ),
        "acoustic_only": sum(
            item["type"] == "acoustic_only" for item in mismatches
        ),
    }
    return {
        "aligned": not mismatches,
        "mismatch_count": len(mismatches),
        "mismatch_counts": counts,
        "max_mismatch_distance_m": (
            _rounded(
                max(item["distance_m"] for item in mismatches)
            )
            if mismatches
            else 0.0
        ),
        "visual_region": {
            "discs": [
                {
                    "pose_id": pose_id,
                    "center_m": [
                        _rounded(center[0]),
                        _rounded(center[1]),
                    ],
                    "radius_m": _rounded(radius),
                }
                for pose_id, center, radius in visual_discs
            ],
            "probe_count": len(probe_specs),
        },
        "acoustic_region": {
            "discs": [
                {
                    "sample_id": sample_id,
                    "center_m": [
                        _rounded(center[0]),
                        _rounded(center[1]),
                    ],
                    "radius_m": _rounded(radius),
                }
                for sample_id, center, radius in acoustic_discs
            ],
            "samples": acoustic_details,
        },
        "mismatches": mismatches,
    }
