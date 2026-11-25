from __future__ import annotations

from typing import Any

from .hashing import canonical_hash


STAGES = ["S1_mask_boundary", "S2_completion", "S3_style", "S4_fusion_blocks", "S5_metrics"]


def task_map(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(task["edit_id"]): task for task in tasks}


def changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    fields = set(old) | set(new)
    result = []
    for field in sorted(fields):
        if field == "revision":
            continue
        if canonical_hash(old.get(field)) != canonical_hash(new.get(field)):
            result.append(field)
    if int(new.get("revision", 0)) <= int(old.get("revision", 0)):
        result.append("revision_not_advanced")
    return result


def plan_revision(
    old_tasks: list[dict[str, Any]],
    new_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    old_by_id = task_map(old_tasks)
    new_by_id = task_map(new_tasks)
    entries = []
    recomputed_edit_ids: set[str] = set()
    fully_reused_edit_ids: set[str] = set()

    for edit_id in sorted(new_by_id):
        new_task = new_by_id[edit_id]
        old_task = old_by_id.get(edit_id)
        if old_task is None:
            fields = ["__new_task__"]
            recompute = list(STAGES)
            reuse: list[str] = []
            recomputed_edit_ids.add(edit_id)
        else:
            fields = changed_fields(old_task, new_task)
            real_fields = [f for f in fields if f != "revision_not_advanced"]
            if "mask" in real_fields:
                recompute = list(STAGES)
                reuse = []
                recomputed_edit_ids.add(edit_id)
            elif real_fields == ["style_strength"]:
                recompute = ["S3_style", "S4_fusion_blocks", "S5_metrics"]
                reuse = ["S1_mask_boundary", "S2_completion"]
                recomputed_edit_ids.add(edit_id)
            elif not real_fields:
                recompute = []
                reuse = list(STAGES)
                fully_reused_edit_ids.add(edit_id)
            else:
                # Conservative DAG invalidation for any other changed input.
                recompute = list(STAGES)
                reuse = []
                recomputed_edit_ids.add(edit_id)
        entries.append({
            "edit_id": edit_id,
            "changed_fields": fields,
            "recompute_stages": recompute,
            "reuse_stages": reuse,
            "block_downstream": [name for name in ("block_style", "block_fusion") if any(
                stage in recompute for stage in ("S3_style", "S4_fusion_blocks")
            )],
        })

    missing = sorted(set(old_by_id) - set(new_by_id))
    return {
        "entries": entries,
        "missing_edit_ids": missing,
        "recomputed_edit_ids": sorted(recomputed_edit_ids),
        "fully_reused_edit_ids": sorted(fully_reused_edit_ids),
        "recompute_set": sorted(recomputed_edit_ids),
        "reuse_set": sorted(fully_reused_edit_ids),
        "sets_disjoint": recomputed_edit_ids.isdisjoint(fully_reused_edit_ids),
        "union_covers_all_tasks": recomputed_edit_ids | fully_reused_edit_ids == set(new_by_id),
    }


def stale_fields(old_key_inputs: dict[str, Any], current_task: dict[str, Any]) -> list[str]:
    stale = []
    direct_fields = [
        "image_seed", "style_strength", "iterations", "block_size",
        "dtype_profile", "revision",
    ]
    for field in direct_fields:
        current_value = current_task.get(field)
        old_value = old_key_inputs.get(field)
        if old_value is not None and canonical_hash(old_value) != canonical_hash(current_value):
            stale.append(field)
    old_mask_digest = old_key_inputs.get("raw_mask_digest")
    if old_mask_digest is not None:
        if old_mask_digest != canonical_hash(current_task.get("mask")):
            stale.append("mask")
    return sorted(stale)


def detect_stale_result(
    edit_id: str,
    cache_record: dict[str, Any],
    current_task: dict[str, Any],
    current_cache_key: str,
) -> dict[str, Any] | None:
    if cache_record.get("cache_key") == current_cache_key:
        return None
    fields = stale_fields(cache_record.get("key_inputs", {}), current_task)
    return {
        "ok": False,
        "code": "E_STALE_RESULT",
        "edit_id": edit_id,
        "stale_fields": fields or ["cache_key"],
        "old_cache_key": cache_record.get("cache_key"),
        "current_cache_key": current_cache_key,
        "old_artifact_hash": cache_record.get("artifact_hash"),
    }
