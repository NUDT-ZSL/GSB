from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .evaluator import assemble_evaluation, evaluate_pose, evaluate_project
from .geometry import dot, subtract
from .loader import project_from_data
from .model import Furniture, Project, ViewingPose
from .optics import projected_screen_coordinates
from .utils import (
    deep_diff,
    read_json,
    write_json_atomic,
)

STATE_SCHEMA = 1


def make_state(project: Project, evaluation: dict[str, Any]) -> dict[str, Any]:
    if evaluation.get("project_fingerprint") != project.fingerprint:
        raise ValueError("evaluation does not belong to the supplied project")
    return {
        "state_schema": STATE_SCHEMA,
        "project_fingerprint": project.fingerprint,
        "project": project.raw,
        "evaluation": evaluation,
    }


def write_state(
    path: str | Path,
    project: Project,
    evaluation: dict[str, Any],
) -> None:
    write_json_atomic(path, make_state(project, evaluation))


def load_state(path: str | Path) -> tuple[Project, dict[str, Any]]:
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError("state root must be an object")
    if data.get("state_schema") != STATE_SCHEMA:
        raise ValueError("unsupported state schema")
    project_data = data.get("project")
    evaluation = data.get("evaluation")
    if not isinstance(project_data, dict) or not isinstance(
        evaluation, dict
    ):
        raise ValueError("state must contain project and evaluation objects")
    project = project_from_data(project_data, source=f"{path}#project")
    if data.get("project_fingerprint") != project.fingerprint:
        raise ValueError("state project fingerprint is inconsistent")
    if evaluation.get("project_fingerprint") != project.fingerprint:
        raise ValueError("state evaluation belongs to a different project")
    return project, evaluation


def _pose_map(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in data.get("viewing_poses", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _furniture_map(
    data: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in data.get("furniture", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _all_block_indices(project: Project) -> set[tuple[int, int]]:
    block_cols, block_rows = project.screen.blocks
    return {
        (row, col)
        for row in range(block_rows)
        for col in range(block_cols)
    }


def _furniture_blocks_for_pose(
    project: Project,
    pose: ViewingPose,
    furniture: Furniture,
) -> set[tuple[int, int]] | None:
    """Conservatively map a furniture AABB projection to screen blocks.

    ``None`` means the projection is unsafe and the caller must recompute all
    blocks for the pose.
    """

    if furniture.id == pose.furniture_id:
        return set()
    if furniture.contains(pose.position, tolerance=1e-6):
        return None
    coordinates = []
    for corner in furniture.corners:
        if dot(subtract(corner, pose.position), pose.forward) <= 0.0:
            return None
        projected = projected_screen_coordinates(project, pose, corner)
        if projected is None:
            return None
        coordinates.append(projected)
    if not coordinates:
        return None
    minimum_u = min(item[0] for item in coordinates)
    maximum_u = max(item[0] for item in coordinates)
    minimum_v = min(item[1] for item in coordinates)
    maximum_v = max(item[1] for item in coordinates)
    if (
        maximum_u < 0.0
        or minimum_u > 1.0
        or maximum_v < 0.0
        or minimum_v > 1.0
    ):
        return set()
    if (
        minimum_u < -3.0
        or maximum_u > 4.0
        or minimum_v < -3.0
        or maximum_v > 4.0
    ):
        return None
    block_cols, block_rows = project.screen.blocks
    col_start = max(
        0, min(block_cols - 1, math.floor(minimum_u * block_cols))
    )
    col_end = max(
        0, min(block_cols - 1, math.floor(maximum_u * block_cols))
    )
    row_start = max(
        0, min(block_rows - 1, math.floor(minimum_v * block_rows))
    )
    row_end = max(
        0, min(block_rows - 1, math.floor(maximum_v * block_rows))
    )
    col_start = max(0, col_start - 1)
    col_end = min(block_cols - 1, col_end + 1)
    row_start = max(0, row_start - 1)
    row_end = min(block_rows - 1, row_end + 1)
    return {
        (row, col)
        for row in range(row_start, row_end + 1)
        for col in range(col_start, col_end + 1)
    }


def infer_affected_blocks(
    base_project: Project,
    changed_project: Project,
) -> tuple[dict[str, Any], dict[str, set[tuple[int, int]]]]:
    changes = deep_diff(
        base_project.raw, changed_project.raw, limit=500
    )
    all_indices = _all_block_indices(changed_project)
    changed_pose_ids = sorted(
        pose.id for pose in changed_project.viewing_poses
    )
    affected: dict[str, set[tuple[int, int]]] = {
        pose_id: set() for pose_id in changed_pose_ids
    }
    reasons: dict[str, list[str]] = {
        pose_id: [] for pose_id in changed_pose_ids
    }
    force_full_reasons: list[str] = []
    for component, label in (
        ("room", "room_geometry_changed"),
        ("screen", "screen_parameters_changed"),
        ("stereo", "stereo_parameters_changed"),
        ("assessment", "assessment_parameters_changed"),
    ):
        if base_project.raw.get(component) != changed_project.raw.get(
            component
        ):
            force_full_reasons.append(label)
    if not changes:
        pass
    if force_full_reasons:
        for pose_id in changed_pose_ids:
            affected[pose_id] = set(all_indices)
            reasons[pose_id].extend(force_full_reasons)

    base_poses = _pose_map(base_project.raw)
    changed_poses = _pose_map(changed_project.raw)
    added_poses = sorted(set(changed_poses) - set(base_poses))
    removed_poses = sorted(set(base_poses) - set(changed_poses))
    modified_poses = sorted(
        pose_id
        for pose_id in set(base_poses) & set(changed_poses)
        if base_poses[pose_id] != changed_poses[pose_id]
    )
    for pose_id in added_poses + modified_poses:
        if pose_id not in affected:
            continue
        affected[pose_id] = set(all_indices)
        reasons[pose_id].append(
            "viewing_pose_added"
            if pose_id in added_poses
            else "viewing_pose_changed"
        )

    base_furniture = _furniture_map(base_project.raw)
    changed_furniture = _furniture_map(changed_project.raw)
    added_furniture = sorted(
        set(changed_furniture) - set(base_furniture)
    )
    removed_furniture = sorted(
        set(base_furniture) - set(changed_furniture)
    )
    modified_furniture = sorted(
        object_id
        for object_id in set(base_furniture) & set(changed_furniture)
        if base_furniture[object_id] != changed_furniture[object_id]
    )
    changed_furniture_ids = (
        added_furniture + removed_furniture + modified_furniture
    )
    changed_pose_map = changed_project.pose_by_id()
    base_pose_map = base_project.pose_by_id()
    base_furniture_map = base_project.furniture_by_id()
    changed_furniture_map = changed_project.furniture_by_id()
    for object_id in changed_furniture_ids:
        for pose_id, changed_pose in changed_pose_map.items():
            if pose_id not in affected:
                continue
            footprint: set[tuple[int, int]] = set()
            unsafe = False
            if object_id in base_furniture_map:
                old_pose = base_pose_map.get(pose_id)
                if old_pose is not None:
                    old_blocks = _furniture_blocks_for_pose(
                        base_project,
                        old_pose,
                        base_furniture_map[object_id],
                    )
                    if old_blocks is None:
                        unsafe = True
                    else:
                        footprint.update(old_blocks)
            if object_id in changed_furniture_map:
                new_blocks = _furniture_blocks_for_pose(
                    changed_project,
                    changed_pose,
                    changed_furniture_map[object_id],
                )
                if new_blocks is None:
                    unsafe = True
                else:
                    footprint.update(new_blocks)
            if unsafe:
                affected[pose_id] = set(all_indices)
                reasons[pose_id].append(
                    f"furniture:{object_id}:unbounded_projection"
                )
            elif footprint:
                affected[pose_id].update(footprint)
                reasons[pose_id].append(f"furniture:{object_id}")

    for pose_id in changed_pose_ids:
        reasons[pose_id] = sorted(set(reasons[pose_id]))
        affected[pose_id] &= all_indices

    ranges_output = []
    recomputed = 0
    for pose_id in changed_pose_ids:
        blocks = affected[pose_id]
        if not blocks:
            continue
        recomputed += len(blocks)
        ranges_output.append(
            {
                "pose_id": pose_id,
                "block_count": len(blocks),
                "reasons": reasons[pose_id],
                "block_ranges": _compress_block_ranges(
                    blocks, changed_project.screen.blocks[0]
                ),
            }
        )
    total = len(changed_pose_ids) * len(all_indices)
    information = {
        "full_recompute": bool(force_full_reasons),
        "force_full_reasons": force_full_reasons,
        "changed_path_count": len(changes),
        "changed_paths": changes,
        "added_pose_ids": added_poses,
        "removed_pose_ids": removed_poses,
        "modified_pose_ids": modified_poses,
        "added_furniture_ids": added_furniture,
        "removed_furniture_ids": removed_furniture,
        "modified_furniture_ids": modified_furniture,
        "poses": ranges_output,
        "recomputed_block_count": recomputed,
        "reused_block_count": total - recomputed,
        "total_block_count": total,
    }
    return information, affected


def _compress_block_ranges(
    blocks: set[tuple[int, int]],
    block_cols: int,
) -> list[dict[str, int]]:
    ranges: list[dict[str, int]] = []
    rows = sorted({row for row, _ in blocks})
    for row in rows:
        columns = sorted(col for current_row, col in blocks if current_row == row)
        start = columns[0]
        previous = columns[0]
        for column in columns[1:]:
            if column == previous + 1:
                previous = column
                continue
            ranges.append(
                {
                    "row": row,
                    "col_start": start,
                    "col_end_exclusive": previous + 1,
                }
            )
            start = previous = column
        ranges.append(
            {
                "row": row,
                "col_start": start,
                "col_end_exclusive": previous + 1,
            }
        )
    return ranges


def incremental_evaluate(
    base_project: Project,
    changed_project: Project,
    *,
    baseline_evaluation: dict[str, Any] | None = None,
    verify_full: bool = True,
) -> dict[str, Any]:
    if base_project.project_id != changed_project.project_id:
        raise ValueError(
            "incremental reassessment requires the same project_id"
        )
    baseline = (
        baseline_evaluation
        if baseline_evaluation is not None
        else evaluate_project(base_project)
    )
    if baseline.get("project_fingerprint") != base_project.fingerprint:
        raise ValueError(
            "baseline evaluation belongs to a different base project"
        )
    affected_info, affected = infer_affected_blocks(
        base_project, changed_project
    )
    baseline_poses = {
        pose["pose_id"]: pose for pose in baseline["poses"]
    }
    pose_summaries: list[dict[str, Any]] = []
    recomputed = 0
    reused = 0
    for pose in sorted(
        changed_project.viewing_poses, key=lambda item: item.id
    ):
        base_pose = baseline_poses.get(pose.id)
        selected = affected.get(pose.id, set())
        recomputed += len(selected)
        if base_pose is None:
            pose_summary = evaluate_pose(changed_project, pose)
            reused += 0
        else:
            pose_summary = evaluate_pose(
                changed_project,
                pose,
                baseline_pose=base_pose,
                recompute_blocks=selected,
            )
            reused += pose_summary["block_count"] - len(selected)
        pose_summaries.append(pose_summary)
    evaluation = assemble_evaluation(
        changed_project, pose_summaries
    )
    affected_info["recomputed_block_count"] = recomputed
    affected_info["reused_block_count"] = reused
    affected_info["total_block_count"] = recomputed + reused
    consistency = None
    if verify_full:
        full_evaluation = evaluate_project(changed_project)
        consistency = compare_evaluations(evaluation, full_evaluation)
    return {
        "result_schema": 1,
        "mode": "incremental",
        "project_id": changed_project.project_id,
        "base_project_fingerprint": base_project.fingerprint,
        "changed_project_fingerprint": changed_project.fingerprint,
        "affected": affected_info,
        "evaluation": evaluation,
        "consistency": consistency,
    }


def compare_evaluations(
    incremental: dict[str, Any],
    full: dict[str, Any],
) -> dict[str, Any]:
    incremental_poses = {
        pose["pose_id"]: pose for pose in incremental.get("poses", [])
    }
    full_poses = {
        pose["pose_id"]: pose for pose in full.get("poses", [])
    }
    differences: list[dict[str, Any]] = []
    compared_blocks = 0
    all_pose_ids = sorted(set(incremental_poses) | set(full_poses))
    for pose_id in all_pose_ids:
        left_pose = incremental_poses.get(pose_id)
        right_pose = full_poses.get(pose_id)
        if left_pose is None or right_pose is None:
            differences.append(
                {
                    "pose_id": pose_id,
                    "block_row": -1,
                    "block_col": -1,
                    "field": "__pose__",
                    "incremental": left_pose is not None,
                    "full": right_pose is not None,
                }
            )
            continue
        left_blocks = {
            (block["row"], block["col"]): block
            for block in left_pose.get("blocks", [])
        }
        right_blocks = {
            (block["row"], block["col"]): block
            for block in right_pose.get("blocks", [])
        }
        for row, col in sorted(set(left_blocks) | set(right_blocks)):
            compared_blocks += 1
            if (row, col) not in left_blocks or (row, col) not in right_blocks:
                differences.append(
                    {
                        "pose_id": pose_id,
                        "block_row": row,
                        "block_col": col,
                        "field": "__block__",
                        "incremental": (row, col) in left_blocks,
                        "full": (row, col) in right_blocks,
                    }
                )
                continue
            for field, left_value, right_value in _leaf_differences(
                left_blocks[(row, col)],
                right_blocks[(row, col)],
                "",
            ):
                differences.append(
                    {
                        "pose_id": pose_id,
                        "block_row": row,
                        "block_col": col,
                        "field": field,
                        "incremental": left_value,
                        "full": right_value,
                    }
                )
    return {
        "consistent": not differences,
        "block_count_compared": compared_blocks,
        "mismatch_count": len(differences),
        "first_divergence": differences[0] if differences else None,
        "differences": differences[:50],
        "difference_list_truncated": len(differences) > 50,
    }


def _leaf_differences(
    left: Any,
    right: Any,
    path: str,
) -> list[tuple[str, Any, Any]]:
    differences: list[tuple[str, Any, Any]] = []
    if type(left) is not type(right):
        return [(path or "<block>", left, right)]
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else key
            if key not in left:
                differences.append((child, None, right[key]))
            elif key not in right:
                differences.append((child, left[key], None))
            else:
                differences.extend(
                    _leaf_differences(left[key], right[key], child)
                )
        return differences
    if isinstance(left, list):
        if len(left) != len(right):
            differences.append(
                (f"{path}.length", len(left), len(right))
            )
        for index, (left_item, right_item) in enumerate(
            zip(left, right)
        ):
            differences.extend(
                _leaf_differences(
                    left_item, right_item, f"{path}[{index}]"
                )
            )
        return differences
    if left != right:
        return [(path or "<block>", left, right)]
    return []
