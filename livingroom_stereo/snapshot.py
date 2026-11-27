from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .evaluator import evaluate_project
from .loader import project_from_data
from .model import Project
from .utils import (
    canonical_json,
    fingerprint_json,
    read_json,
    safe_version_name,
    write_json_atomic,
)

SNAPSHOT_SCHEMA = 1
INDEX_SCHEMA = 1


def _project_component(project_id: str) -> str:
    if (
        project_id
        and all(
            character.isalnum() or character in "._-"
            for character in project_id
        )
        and project_id not in {".", ".."}
    ):
        return project_id
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:12]
    return f"project-{digest}"


def snapshot_path(
    store: str | Path,
    project_id: str,
    version: str,
) -> Path:
    safe_version = safe_version_name(version)
    return (
        Path(store)
        / _project_component(project_id)
        / f"{safe_version}.json"
    )


def index_path(store: str | Path, project_id: str) -> Path:
    return Path(store) / _project_component(project_id) / "index.json"


def _persisted_summary(
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    comfort_scores: dict[str, Any] = {}
    overlimit_masks: dict[str, Any] = {}
    for pose in evaluation.get("poses", []):
        pose_id = pose["pose_id"]
        comfort_scores[pose_id] = [
            {
                "row": block["row"],
                "col": block["col"],
                "score": block["comfort_score"],
            }
            for block in pose.get("blocks", [])
        ]
        overlimit_masks[pose_id] = [
            {
                "row": block["row"],
                "col": block["col"],
                "mask": block["overlimit_mask"],
            }
            for block in pose.get("blocks", [])
        ]
    return {
        "comfort_scores": comfort_scores,
        "overlimit_masks": overlimit_masks,
        "alignment": evaluation.get("alignment", {}),
        "summary": evaluation.get("summary", {}),
    }


def save_snapshot(
    store: str | Path,
    project: Project,
    version: str,
    *,
    evaluation: dict[str, Any] | None = None,
) -> Path:
    safe_version = safe_version_name(version)
    evaluation_value = evaluation or evaluate_project(project)
    if (
        evaluation_value.get("project_fingerprint")
        != project.fingerprint
    ):
        raise ValueError("evaluation belongs to a different project")
    snapshot = {
        "snapshot_schema": SNAPSHOT_SCHEMA,
        "project_id": project.project_id,
        "version": safe_version,
        "project_fingerprint": project.fingerprint,
        "project": project.raw,
        "evaluation": evaluation_value,
        "persisted": _persisted_summary(evaluation_value),
    }
    target = snapshot_path(store, project.project_id, safe_version)
    write_json_atomic(target, snapshot)
    _update_index(store, project.project_id, safe_version, target, snapshot)
    return target


def load_snapshot(
    store: str | Path,
    project_id: str,
    version: str,
) -> dict[str, Any]:
    path = snapshot_path(store, project_id, version)
    snapshot = read_json(path)
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot root must be an object")
    if snapshot.get("snapshot_schema") != SNAPSHOT_SCHEMA:
        raise ValueError("unsupported snapshot schema")
    if snapshot.get("project_id") != project_id:
        raise ValueError("snapshot project_id does not match query")
    if snapshot.get("version") != safe_version_name(version):
        raise ValueError("snapshot version does not match query")
    if not isinstance(snapshot.get("project"), dict) or not isinstance(
        snapshot.get("evaluation"), dict
    ):
        raise ValueError("snapshot is missing project or evaluation")
    project = project_from_data(
        snapshot["project"], source=f"{path}#project"
    )
    if snapshot.get("project_fingerprint") != project.fingerprint:
        raise ValueError("snapshot project fingerprint is inconsistent")
    if (
        snapshot["evaluation"].get("project_fingerprint")
        != project.fingerprint
    ):
        raise ValueError("snapshot evaluation belongs to another project")
    return snapshot


def load_snapshot_state(
    store: str | Path,
    project_id: str,
    version: str,
) -> tuple[Project, dict[str, Any]]:
    snapshot = load_snapshot(store, project_id, version)
    project = project_from_data(
        snapshot["project"],
        source=str(snapshot_path(store, project_id, version)),
    )
    return project, snapshot["evaluation"]


def _update_index(
    store: str | Path,
    project_id: str,
    version: str,
    target: Path,
    snapshot: dict[str, Any],
) -> None:
    index_file = index_path(store, project_id)
    versions: dict[str, dict[str, Any]] = {}
    if index_file.exists():
        try:
            existing = read_json(index_file)
        except (OSError, json.JSONDecodeError):
            existing = {}
        if (
            isinstance(existing, dict)
            and existing.get("index_schema") == INDEX_SCHEMA
            and isinstance(existing.get("versions"), list)
        ):
            for entry in existing["versions"]:
                if isinstance(entry, dict) and isinstance(
                    entry.get("version"), str
                ):
                    versions[entry["version"]] = entry
    digest = hashlib.sha256(
        canonical_json(snapshot).encode("utf-8")
    ).hexdigest()
    versions[version] = {
        "version": version,
        "project_fingerprint": snapshot["project_fingerprint"],
        "snapshot_sha256": digest,
        "file": target.name,
    }
    index_value = {
        "index_schema": INDEX_SCHEMA,
        "project_id": project_id,
        "versions": [
            versions[key] for key in sorted(versions)
        ],
    }
    write_json_atomic(index_file, index_value)


def list_snapshots(
    store: str | Path,
    project_id: str,
) -> dict[str, Any]:
    index_file = index_path(store, project_id)
    if not index_file.exists():
        return {
            "index_schema": INDEX_SCHEMA,
            "project_id": project_id,
            "versions": [],
        }
    data = read_json(index_file)
    if not isinstance(data, dict):
        raise ValueError("snapshot index root must be an object")
    return data


def _normalise_pose_ids(
    pose_ids: Iterable[str] | None,
) -> set[str] | None:
    if pose_ids is None:
        return None
    return set(pose_ids)


def _in_range(
    value: int,
    bounds: tuple[int, int] | None,
) -> bool:
    if bounds is None:
        return True
    return bounds[0] <= value <= bounds[1]


def query_snapshot(
    store: str | Path,
    project_id: str,
    version: str,
    *,
    pose_ids: Iterable[str] | None = None,
    row_range: tuple[int, int] | None = None,
    col_range: tuple[int, int] | None = None,
) -> dict[str, Any]:
    snapshot = load_snapshot(store, project_id, version)
    selected_poses = _normalise_pose_ids(pose_ids)
    blocks: list[dict[str, Any]] = []
    pose_outputs: list[dict[str, Any]] = []
    for pose in snapshot["evaluation"].get("poses", []):
        if selected_poses is not None and pose["pose_id"] not in selected_poses:
            continue
        filtered_blocks = [
            {
                "row": block["row"],
                "col": block["col"],
                "block_id": block["block_id"],
                "comfort_score": block["comfort_score"],
                "overlimit": block["overlimit"],
                "primary_cause": block["primary_cause"],
                "limit_causes": block["limit_causes"],
                "parallax_statistics": block["parallax_statistics"],
                "depth_statistics": block["depth_statistics"],
                "overlimit_mask": block["overlimit_mask"],
                "parallax_overlimit_mask": block[
                    "parallax_overlimit_mask"
                ],
                "depth_overlimit_mask": block["depth_overlimit_mask"],
            }
            for block in pose.get("blocks", [])
            if _in_range(block["row"], row_range)
            and _in_range(block["col"], col_range)
        ]
        blocks.extend(
            {
                "pose_id": pose["pose_id"],
                **block,
            }
            for block in filtered_blocks
        )
        pose_outputs.append(
            {
                "pose_id": pose["pose_id"],
                "comfortable_block_count": pose[
                    "comfortable_block_count"
                ],
                "overlimit_block_count": pose[
                    "overlimit_block_count"
                ],
                "mean_comfort_score": pose["mean_comfort_score"],
                "blocks": filtered_blocks,
            }
        )
    return {
        "project_id": project_id,
        "version": version,
        "project_fingerprint": snapshot["project_fingerprint"],
        "query": {
            "pose_ids": (
                sorted(selected_poses)
                if selected_poses is not None
                else None
            ),
            "row_range": (
                {
                    "min": row_range[0],
                    "max": row_range[1],
                }
                if row_range is not None
                else None
            ),
            "col_range": (
                {
                    "min": col_range[0],
                    "max": col_range[1],
                }
                if col_range is not None
                else None
            ),
            "range_semantics": "inclusive",
        },
        "summary": snapshot["evaluation"].get("summary", {}),
        "poses": pose_outputs,
        "blocks": blocks,
        "alignment": snapshot.get("persisted", {}).get(
            "alignment", snapshot["evaluation"].get("alignment", {})
        ),
    }


def snapshot_identity(snapshot: dict[str, Any]) -> str:
    """Stable fingerprint used by tests and change reporting."""

    return fingerprint_json(
        {
            "project_fingerprint": snapshot.get("project_fingerprint"),
            "evaluation": snapshot.get("evaluation"),
            "persisted": snapshot.get("persisted"),
        }
    )
