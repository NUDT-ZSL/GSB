from __future__ import annotations

import math
from typing import Any

import numpy as np

from .blocks import build_cache_key, stitch_blocks
from .hashing import array_digest
from .pipeline import build_context, generate_block_pixels, run_global


def pixel_multiset_digest(image: np.ndarray) -> str:
    flattened = np.ascontiguousarray(image).reshape(-1)
    ordered = np.sort(flattened)
    return array_digest(ordered.reshape(-1, 1))


def independent_condition_recompute(task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    source_context = build_context(task, config, reference=None, anchor_strength=0.0)
    first = run_global(task, config, context=source_context)
    recomputed = run_global(
        task,
        config,
        reference=None,
        anchor_strength=0.0,
        condition_override=source_context.condition,
    )
    digest_match = first["image_digest"] == recomputed["image_digest"]
    byte_match = first["image"].tobytes() == recomputed["image"].tobytes()
    return {
        "ok": digest_match and byte_match,
        "condition_dim": int(source_context.condition.size),
        "condition_vector_checksum": source_context.condition_record.get("vector_checksum"),
        "fusion_checksum": source_context.fusion_resolved["checksum"],
        "iteration_update_order": config["iteration"]["update_order"],
        "sampler_steps": int(task["sampler_steps"]),
        "first_digest": first["image_digest"],
        "recomputed_digest": recomputed["image_digest"],
        "bytewise_match": byte_match,
    }


def cache_key_audit(service, task_id: str) -> dict[str, Any]:
    task = service.get_task(task_id)
    plan = service.active_plan
    reference = service._references.get(task_id)  # noqa: SLF001 - internal audit
    reference_hash = array_digest(reference) if reference is not None else None
    records = [record for record in service.cache.records()
               if record["metadata"].get("task_id") == task_id]
    issues = []
    checked = 0
    descriptor_by_index = {int(block["block_index"]): block for block in plan["blocks"]}
    context = service.get_context(task_id)
    for record in records:
        metadata = record["metadata"]
        index = int(metadata["block_index"])
        descriptor = descriptor_by_index[index]
        expected_key = service._cache_key(task, descriptor, context)  # noqa: SLF001
        checked += 1
        if expected_key != record["cache_key"]:
            issues.append({"block_index": index, "problem": "cache_key_mismatch"})
    hash_report = service.cache.verify_product_hashes()
    issues.extend(hash_report["hash_mismatches"])
    return {"ok": not issues and hash_report["ok"], "checked": checked, "issues": issues,
            "hash_report": hash_report}


def reuse_recompute_coverage(service, task_id: str) -> dict[str, Any]:
    plan = service.active_plan
    if plan is None or service.active_task_id != task_id:
        plan = service._require_active_plan(task_id)  # noqa: SLF001
    all_indices = {int(block["block_index"]) for block in plan["blocks"]}
    cached_indices = set(service.cache.products_by_index(task_id).keys())
    if cached_indices != all_indices:
        return {
            "ok": False,
            "reason": "not_all_blocks_are_cached",
            "missing": sorted(all_indices - cached_indices),
        }

    ordered = sorted(all_indices)
    split = max(1, math.ceil(len(ordered) / 2))
    reused_indices = set(ordered[:split])
    recomputed_indices = set(ordered[split:])
    if not recomputed_indices and len(ordered) > 1:
        recomputed_indices = {ordered[-1]}
        reused_indices.discard(ordered[-1])

    context = service.get_context(task_id)
    global_result = service.generate_global(task_id)
    descriptor_by_index = {int(block["block_index"]): block for block in plan["blocks"]}
    recompute_hashes = {}
    for index in sorted(recomputed_indices):
        descriptor = descriptor_by_index[index]
        product = generate_block_pixels(
            context,
            descriptor["row_start"], descriptor["row_end"],
            descriptor["col_start"], descriptor["col_end"],
        )
        expected = global_result["image"][
            descriptor["row_start"]:descriptor["row_end"],
            descriptor["col_start"]:descriptor["col_end"],
        ]
        if product["image_digest"] != array_digest(expected):
            return {
                "ok": False,
                "reason": "recomputed_block_differs",
                "block_index": index,
            }
        recompute_hashes[str(index)] = product["image_digest"]

    intersection = reused_indices & recomputed_indices
    union = reused_indices | recomputed_indices
    return {
        "ok": not intersection and union == all_indices,
        "reused_indices": sorted(reused_indices),
        "recomputed_indices": sorted(recomputed_indices),
        "intersection": sorted(intersection),
        "union_covers_all_blocks": union == all_indices,
        "disjoint": not intersection,
        "recomputed_hashes": recompute_hashes,
    }


def run_cross_module_self_check(service, task_id: str) -> dict[str, Any]:
    verification = service.verify_blocks(task_id)
    task = service.get_task(task_id)
    plan = service.active_plan
    stitched, _, _ = service.stitch_cached(task_id)
    global_result = service.generate_global(task_id)

    stitch_digest = array_digest(stitched)
    global_digest = array_digest(global_result["image"])
    multiset_match = pixel_multiset_digest(stitched) == pixel_multiset_digest(global_result["image"])
    key_audit = cache_key_audit(service, task_id)
    recompute = independent_condition_recompute(task, service.config)
    coverage = reuse_recompute_coverage(service, task_id)

    report = {
        "task_id": task_id,
        "ok": bool(
            verification["ok"]
            and stitch_digest == global_digest
            and multiset_match
            and key_audit["ok"]
            and recompute["ok"]
            and coverage["ok"]
        ),
        "pixel_set_consistency": {
            "exact_array_equal": bool(np.array_equal(stitched, global_result["image"])),
            "bytewise_equal": verification["bytewise_equal"],
            "order_independent_multiset_match": multiset_match,
            "stitched_digest": stitch_digest,
            "global_digest": global_digest,
        },
        "cache_key_and_hash_audit": key_audit,
        "independent_condition_recompute": recompute,
        "reuse_recompute_coverage": coverage,
        "verification": {
            "ok": verification["ok"],
            "bytewise_equal": verification["bytewise_equal"],
            "mismatch": verification.get("mismatch"),
        },
        "plan_grid": plan["grid"] if plan else None,
    }
    return report
