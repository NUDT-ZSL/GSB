"""Content-addressed dependency invalidation for v2 config revisions."""

from __future__ import annotations

from typing import Any

import numpy as np

from .hashing import content_key
from .ledger import iter_pixel_coordinates, pixel_id, region_id, row_dependency_key
from .models import UnifiedConfig
from .normalization import normalize_v2


def diff_v2_configs(old: UnifiedConfig, new: UnifiedConfig) -> dict[str, Any]:
    old_maps = {item.instance_id: item for item in old.mappings}
    new_maps = {item.instance_id: item for item in new.mappings}
    mapping_changes: list[dict[str, Any]] = []
    threshold_changes: list[dict[str, Any]] = []
    added: list[int] = sorted(set(new_maps) - set(old_maps))
    removed: list[int] = sorted(set(old_maps) - set(new_maps))

    for instance_id in sorted(set(old_maps) & set(new_maps)):
        before = old_maps[instance_id]
        after = new_maps[instance_id]
        if before.category_id != after.category_id:
            mapping_changes.append(
                {
                    "instance_id": instance_id,
                    "old_category_id": before.category_id,
                    "new_category_id": after.category_id,
                }
            )
        if before.confidence_threshold != after.confidence_threshold:
            threshold_changes.append(
                {
                    "instance_id": instance_id,
                    "old_threshold": before.confidence_threshold,
                    "new_threshold": after.confidence_threshold,
                }
            )

    default_changed = old.default_confidence_threshold != new.default_confidence_threshold
    pixel_relevant = bool(mapping_changes or threshold_changes or added or removed or default_changed)
    if mapping_changes or added or removed:
        stage_suffix = [
            "normalize_v2",
            "threshold_decision",
            "adjudicate_rows",
            "build_ledger_checksum",
            "self_check",
        ]
    elif threshold_changes or default_changed:
        stage_suffix = [
            "threshold_decision",
            "adjudicate_rows",
            "build_ledger_checksum",
            "self_check",
        ]
    else:
        stage_suffix = ["config_snapshot_metadata"]

    return {
        "old_checksum": old.checksum,
        "new_checksum": new.checksum,
        "mapping_changes": mapping_changes,
        "threshold_changes": threshold_changes,
        "added_instances": added,
        "removed_instances": removed,
        "default_threshold_changed": default_changed,
        "pixel_relevant": pixel_relevant,
        "invalidated_stage_suffix": stage_suffix,
    }


def _truth_for_pixel(arrays: dict[str, np.ndarray], y: int, x: int) -> list[dict[str, Any]]:
    truth: list[dict[str, Any]] = []
    templates = arrays["templates"]
    for index in range(templates.shape[0]):
        if int(templates[index, y, x]) != 0:
            truth.append(
                {
                    "instance_id": int(arrays["template_instance_ids"][index]),
                    "category_id": int(arrays["template_category_ids"][index]),
                }
            )
    return truth


def plan_revision(
    arrays: dict[str, np.ndarray],
    old_v1: UnifiedConfig,
    old_v2: UnifiedConfig,
    new_v2: UnifiedConfig,
    *,
    threshold_strategy: str = "strict",
    tolerance: float = 1e-12,
    region_size: int = 4,
) -> dict[str, Any]:
    height, width = arrays["gt_instance"].shape
    diff = diff_v2_configs(old_v2, new_v2)
    coordinates = iter_pixel_coordinates(height, width)
    affected_rows: list[str] = []
    old_keys: dict[str, str] = {}
    new_keys: dict[str, str] = {}
    first_dependency_delta: dict[str, Any] | None = None

    for y, x in coordinates:
        truth = _truth_for_pixel(arrays, y, x)
        common = {
            "y": y,
            "x": x,
            "truth": truth,
            "v1_instance": int(arrays["v1_instance"][y, x]),
            "v1_config": old_v1,
            "v2_raw_instance": int(arrays["v2_instance"][y, x]),
            "v2_raw_category": int(arrays["v2_category"][y, x]),
            "v2_confidence": float(arrays["v2_confidence"][y, x]),
            "threshold_strategy": threshold_strategy,
            "tolerance": tolerance,
        }
        old_key = row_dependency_key(**common, v2_config=old_v2)
        new_key = row_dependency_key(**common, v2_config=new_v2)
        row_id = pixel_id(y, x)
        old_keys[row_id] = old_key
        new_keys[row_id] = new_key
        if old_key != new_key:
            affected_rows.append(row_id)
            if first_dependency_delta is None:
                first_dependency_delta = {"row_id": row_id, "y": y, "x": x}

    all_rows = [pixel_id(y, x) for y, x in coordinates]
    affected_set = set(affected_rows)
    recompute_set = list(affected_rows)
    reuse_set = [row_id for row_id in all_rows if row_id not in affected_set]
    affected_regions = sorted(
        {region_id(*map(int, row_id.removeprefix("P:").split(":")), region_size) for row_id in affected_rows}
    )
    all_regions = sorted(
        {region_id(y, x, region_size) for y, x in coordinates}
    )
    clean_regions = sorted(set(all_regions) - set(affected_regions))

    old_norm = normalize_v2(
        arrays["v2_category"], arrays["v2_instance"], arrays["v2_confidence"], old_v2,
        strategy=threshold_strategy, tolerance=tolerance,
    )
    new_norm = normalize_v2(
        arrays["v2_category"], arrays["v2_instance"], arrays["v2_confidence"], new_v2,
        strategy=threshold_strategy, tolerance=tolerance,
    )
    acceptance_delta = old_norm["accepted"] != new_norm["accepted"]
    category_delta = old_norm["category"] != new_norm["category"]
    semantic_delta = acceptance_delta | category_delta
    delta_positions = np.argwhere(semantic_delta)
    first_semantic_delta = None
    if delta_positions.size:
        y, x = [int(v) for v in delta_positions[0]]
        first_semantic_delta = {"row_id": pixel_id(y, x), "y": y, "x": x}

    coverage = {
        "recompute_count": len(recompute_set),
        "reuse_count": len(reuse_set),
        "total_count": len(all_rows),
        "disjoint": set(recompute_set).isdisjoint(reuse_set),
        "union_all_rows": set(recompute_set).union(reuse_set) == set(all_rows),
        "affected_region_count": len(affected_regions),
        "region_disjoint": set(affected_regions).isdisjoint(clean_regions),
        "region_union_all": set(affected_regions).union(clean_regions) == set(all_regions),
        "full_image_recompute": len(recompute_set) == len(all_rows),
    }
    coverage["covered"] = coverage["disjoint"] and coverage["union_all_rows"]

    return {
        "plan_format": "seg-diag-dependency-plan/v1",
        "plan_checksum": content_key(
            {
                "old_v2": old_v2.checksum,
                "new_v2": new_v2.checksum,
                "affected_rows": recompute_set,
                "strategy": threshold_strategy,
            }
        ),
        **diff,
        "cache_key_algorithm": "sha256(canonical_json(row_semantic_dependencies))",
        "old_config_cache_key": old_v2.checksum,
        "new_config_cache_key": new_v2.checksum,
        "affected_pixel_count": len(affected_rows),
        "affected_row_set": recompute_set,
        "recompute_set": recompute_set,
        "reuse_set": reuse_set,
        "affected_regions": affected_regions,
        "recompute_regions": affected_regions,
        "reuse_regions": clean_regions,
        "all_regions": all_regions,
        "coverage_check": coverage,
        "first_dependency_delta": first_dependency_delta,
        "semantic_delta_pixel_count": int(semantic_delta.sum()),
        "acceptance_delta_pixel_count": int(acceptance_delta.sum()),
        "category_delta_pixel_count": int(category_delta.sum()),
        "first_semantic_delta": first_semantic_delta,
        "old_row_keys": old_keys,
        "new_row_keys": new_keys,
    }
