"""Exact region-preview versus batch consistency adjudication."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .heatmap import compare_peak_sets
from .io_utils import canonical_json_bytes
from .models import Instance, Peak, RegionSpec


def canonical_instances(
    instances: Iterable[Instance], score_decimals: int = 9
) -> list[dict[str, Any]]:
    return [
        instance.canonical(score_decimals)
        for instance in sorted(instances, key=lambda item: item.instance_id)
    ]


def compare_region(
    full_instances: Iterable[Instance],
    preview_instances: Iterable[Instance],
    full_peaks: Iterable[Peak],
    preview_peaks: Iterable[Peak],
    region: RegionSpec,
    *,
    score_decimals: int = 9,
    scene_id: str | None = None,
) -> dict[str, Any]:
    peak_verdict = compare_peak_sets(
        full_peaks,
        preview_peaks,
        region,
        scene_id=scene_id,
    )
    if peak_verdict["business_code"] != "OK":
        return {
            **peak_verdict,
            "verdict": "FAIL",
            "stage": "refine",
        }
    full_region = [
        instance
        for instance in full_instances
        if any(
            region.contains(float(peak.x), float(peak.y))
            for peak in instance.peaks
        )
    ]
    preview_region = [
        instance
        for instance in preview_instances
        if any(
            region.contains(float(peak.x), float(peak.y))
            for peak in instance.peaks
        )
    ]
    full = canonical_instances(full_region, score_decimals)
    preview = canonical_instances(preview_region, score_decimals)
    full_bytes = canonical_json_bytes(full)
    preview_bytes = canonical_json_bytes(preview)
    if full_bytes == preview_bytes:
        return {
            "business_code": "OK",
            "consistent": True,
            "verdict": "PASS",
            "region_id": region.region_id,
            "instance_count": len(full),
            "byte_length": len(full_bytes),
        }

    full_map = {item["instance_id"]: item for item in full}
    preview_map = {item["instance_id"]: item for item in preview}
    first_id = sorted(set(full_map) | set(preview_map))[0]
    for candidate in sorted(set(full_map) | set(preview_map)):
        if full_map.get(candidate) != preview_map.get(candidate):
            first_id = candidate
            break
    difference_type = _difference_type(
        full_map.get(first_id), preview_map.get(first_id)
    )
    return {
        "business_code": "E_BLOCK_MISMATCH",
        "consistent": False,
        "verdict": "FAIL",
        "region_id": region.region_id,
        "scene_id": scene_id,
        "first_difference": {
            "instance_id": first_id,
            "difference_type": difference_type,
            "full": full_map.get(first_id),
            "preview": preview_map.get(first_id),
        },
    }


def _difference_type(full: dict[str, Any] | None, preview: dict[str, Any] | None) -> str:
    if full is None:
        return "preview_only"
    if preview is None:
        return "batch_only"
    if full.get("keypoints") != preview.get("keypoints"):
        return "keypoints"
    if full.get("score") != preview.get("score"):
        return "score"
    if full.get("matched") != preview.get("matched"):
        return "matched"
    return "unknown"


def merge_region_instances(
    region_instances: Iterable[Iterable[Instance]],
    *,
    score_decimals: int = 9,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    merged: dict[str, dict[str, Any]] = {}
    for instances in region_instances:
        for instance in instances:
            payload = instance.canonical(score_decimals)
            digest = canonical_json_bytes(payload)
            existing = merged.get(instance.instance_id)
            if existing is not None and canonical_json_bytes(existing) != digest:
                return [], {
                    "business_code": "E_BLOCK_MISMATCH",
                    "difference_type": "conflicting_region_copy",
                    "instance_id": instance.instance_id,
                    "first_copy": existing,
                    "second_copy": payload,
                }
            merged[instance.instance_id] = payload
    return [merged[key] for key in sorted(merged)], None


def compare_stitched(
    full_instances: Iterable[Instance],
    region_instance_groups: Iterable[Iterable[Instance]],
    *,
    score_decimals: int = 9,
    scene_id: str | None = None,
) -> dict[str, Any]:
    stitched, conflict = merge_region_instances(
        region_instance_groups, score_decimals=score_decimals
    )
    if conflict is not None:
        return {
            **conflict,
            "scene_id": scene_id,
            "consistent": False,
            "verdict": "FAIL",
        }
    full = canonical_instances(full_instances, score_decimals)
    if canonical_json_bytes(full) == canonical_json_bytes(stitched):
        return {
            "business_code": "OK",
            "consistent": True,
            "verdict": "PASS",
            "scene_id": scene_id,
            "instance_count": len(full),
            "byte_length": len(canonical_json_bytes(full)),
        }
    full_map = {item["instance_id"]: item for item in full}
    stitched_map = {item["instance_id"]: item for item in stitched}
    first_id = sorted(set(full_map) | set(stitched_map))[0]
    for candidate in sorted(set(full_map) | set(stitched_map)):
        if full_map.get(candidate) != stitched_map.get(candidate):
            first_id = candidate
            break
    return {
        "business_code": "E_BLOCK_MISMATCH",
        "consistent": False,
        "verdict": "FAIL",
        "scene_id": scene_id,
        "first_difference": {
            "instance_id": first_id,
            "difference_type": _difference_type(
                full_map.get(first_id), stitched_map.get(first_id)
            ),
            "full": full_map.get(first_id),
            "stitched": stitched_map.get(first_id),
        },
    }

