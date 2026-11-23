"""Directed cache invalidation after versioned snapshot changes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import PipelineConfig
from .models import Instance, RegionSpec
from .snapshots import SnapshotBundle


def _all_regions(config: PipelineConfig, canvas_shape: tuple[int, int, int]) -> list[str]:
    regions: list[str] = []
    for row in range(config.region_rows):
        for col in range(config.region_cols):
            regions.append(f"r{row:02d}c{col:02d}")
    return regions


def _regions_for_bounds(
    config: PipelineConfig,
    canvas_shape: tuple[int, int, int],
    bbox: tuple[float, float, float, float] | None,
) -> list[str]:
    if bbox is None:
        return _all_regions(config, canvas_shape)
    x0, y0, x1, y1 = bbox
    affected: list[str] = []
    for row in range(config.region_rows):
        for col in range(config.region_cols):
            rx0, ry0, rx1, ry1 = config.region_bbox(row, col, canvas_shape)
            if rx1 <= x0 or rx0 >= x1 or ry1 <= y0 or ry0 >= y1:
                continue
            affected.append(f"r{row:02d}c{col:02d}")
    return affected


def _person_bounds(
    metadata: dict[str, Any], person_ids: set[str]
) -> tuple[float, float, float, float] | None:
    points: list[tuple[float, float]] = []
    for person in metadata.get("persons", []):
        if person.get("person_id") not in person_ids:
            continue
        for keypoint in person.get("keypoints", []):
            points.append((float(keypoint["x"]), float(keypoint["y"])))
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs) - 1.0, min(ys) - 1.0, max(xs) + 1.0, max(ys) + 1.0


def _changed_persons(old: dict[str, Any], new: dict[str, Any]) -> set[str]:
    old_people = {
        person.get("person_id"): person for person in old.get("persons", [])
    }
    new_people = {
        person.get("person_id"): person for person in new.get("persons", [])
    }
    changed: set[str] = set()
    for person_id in set(old_people) | set(new_people):
        if old_people.get(person_id) != new_people.get(person_id):
            changed.add(str(person_id))
    return changed


def _metadata_structural(old: dict[str, Any], new: dict[str, Any]) -> bool:
    for field_name in (
        "edges",
        "link_distance",
        "min_keypoints",
        "max_instance_diameter",
        "channels",
    ):
        if old.get(field_name) != new.get(field_name):
            return True
    return False


def _instances_for_regions(
    instances: Iterable[Instance], region_ids: set[str]
) -> set[str]:
    if not instances:
        return set()
    selected: set[str] = set()
    for instance in instances:
        for peak in instance.peaks:
            region_id = None
            # The caller passes region IDs; deriving coordinates here would need
            # config. Matching is done by the service for coordinate-aware plans.
            if region_id in region_ids:
                selected.add(instance.instance_id)
    return selected


def analyze_invalidation(
    old: SnapshotBundle,
    new: SnapshotBundle,
    config: PipelineConfig,
    *,
    prior_instances: Iterable[Instance] | None = None,
) -> dict[str, Any]:
    changed_categories = [
        category
        for category in old.records
        if old.records[category].version != new.records[category].version
        or old.records[category].checksum != new.records[category].checksum
    ]
    canvas_shape = (
        int(new.heatmaps.shape[1]),
        int(new.heatmaps.shape[2]),
        3,
    )
    all_region_ids = _all_regions(config, canvas_shape)
    all_instance_ids = sorted(
        instance.instance_id for instance in (prior_instances or [])
    )
    if not changed_categories:
        return {
            "business_code": "OK",
            "change_category": "none",
            "changed_categories": [],
            "affected_regions": [],
            "affected_instances": [],
            "reusable_stages": ["reconstruct", "peaks", "refine", "group", "score", "metrics"],
            "invalidated_stages": [],
        }
    if "heatmap_observation" in changed_categories:
        return {
            "business_code": "OK",
            "change_category": "heatmap_observation",
            "changed_categories": changed_categories,
            "affected_regions": all_region_ids,
            "affected_instances": all_instance_ids,
            "reusable_stages": [],
            "invalidated_stages": ["peaks", "group", "score", "metrics"],
        }
    if "keypoint_metadata" in changed_categories:
        old_metadata = old.metadata
        new_metadata = new.metadata
        if _metadata_structural(old_metadata, new_metadata):
            affected_regions = all_region_ids
        else:
            changed_persons = _changed_persons(old_metadata, new_metadata)
            old_bounds = _person_bounds(old_metadata, changed_persons)
            new_bounds = _person_bounds(new_metadata, changed_persons)
            bounds = _union_bounds(old_bounds, new_bounds)
            affected_regions = _regions_for_bounds(
                config,
                canvas_shape,
                bounds,
            )
        affected_instances = sorted(
            instance.instance_id
            for instance in (prior_instances or [])
            if _instance_touches_regions(
                instance, affected_regions, config, canvas_shape
            )
        )
        return {
            "business_code": "OK",
            "change_category": "keypoint_metadata",
            "changed_categories": changed_categories,
            "affected_regions": affected_regions,
            "affected_instances": affected_instances,
            "reusable_stages": ["reconstruct", "peaks", "refine"],
            "invalidated_stages": ["group", "score", "metrics"],
        }
    return {
        "business_code": "OK",
        "change_category": "scoring",
        "changed_categories": changed_categories,
        "affected_regions": all_region_ids,
        "affected_instances": all_instance_ids,
        "reusable_stages": ["reconstruct", "peaks", "refine", "group"],
        "invalidated_stages": ["score", "metrics"],
    }


def _instance_touches_regions(
    instance: Instance,
    region_ids: Iterable[str],
    config: PipelineConfig,
    canvas_shape: tuple[int, int, int],
) -> bool:
    wanted = set(region_ids)
    for row in range(config.region_rows):
        for col in range(config.region_cols):
            region = RegionSpec(
                row,
                col,
                *config.region_bbox(row, col, canvas_shape),
            )
            if f"r{row:02d}c{col:02d}" not in wanted:
                continue
            if any(
                region.contains(float(peak.x), float(peak.y))
                for peak in instance.peaks
            ):
                return True
    return False


def _union_bounds(
    left: tuple[float, float, float, float] | None,
    right: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if left is None:
        return right
    if right is None:
        return left
    return (
        min(left[0], right[0]),
        min(left[1], right[1]),
        max(left[2], right[2]),
        max(left[3], right[3]),
    )

