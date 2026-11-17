"""Voxel-level deterministic global point-cloud merging."""

from __future__ import annotations

import hashlib
import math
from typing import Any

from .canonical import canonical_float, canonical_json


REPRESENTATIVE_RULE = (
    "Within each voxel, sort members lexicographically by finite (x,y,z,point_id); "
    "the first member is the representative. Cloud points are then ordered by "
    "(minimum_source_view, maximum_source_view, voxel_x, voxel_y, voxel_z, representative_point_id)."
)


def merge_global_cloud(
    pair_results: list[dict[str, Any]],
    voxel_size: float,
) -> dict[str, Any]:
    if not math.isfinite(voxel_size) or voxel_size <= 0.0:
        raise ValueError("voxel_size must be finite and positive")
    members: list[dict[str, Any]] = []
    for pair in pair_results:
        if pair.get("business_code"):
            continue
        for point in pair.get("points", []):
            xyz = [float(value) for value in point["xyz"]]
            if not all(math.isfinite(value) for value in xyz):
                continue
            voxel = tuple(int(math.floor(value / voxel_size)) for value in xyz)
            members.append(
                {
                    "voxel": voxel,
                    "xyz": xyz,
                    "point_id": str(point["point_id"]),
                    "source_views": [int(value) for value in point["source_views"]],
                }
            )
    groups: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for member in members:
        groups.setdefault(member["voxel"], []).append(member)

    cloud_points: list[dict[str, Any]] = []
    for voxel, group in groups.items():
        ordered = sorted(
            group,
            key=lambda item: (
                item["xyz"][0],
                item["xyz"][1],
                item["xyz"][2],
                item["point_id"],
            ),
        )
        representative = ordered[0]
        source_views = sorted(
            set(view for member in ordered for view in member["source_views"])
        )
        cloud_points.append(
            {
                "voxel": [voxel[0], voxel[1], voxel[2]],
                "xyz": [canonical_float(value, 10) for value in representative["xyz"]],
                "representative_point_id": representative["point_id"],
                "merged_point_count": len(ordered),
                "source_views": source_views,
                "member_point_ids": sorted(member["point_id"] for member in ordered),
                "_sort": (
                    source_views[0],
                    source_views[-1],
                    voxel[0],
                    voxel[1],
                    voxel[2],
                    representative["point_id"],
                ),
            }
        )
    cloud_points.sort(key=lambda item: item["_sort"])
    for point in cloud_points:
        del point["_sort"]
    points_digest = hashlib.sha256(
        canonical_json(
            [
                {
                    "voxel": point["voxel"],
                    "xyz": point["xyz"],
                    "representative_point_id": point["representative_point_id"],
                    "merged_point_count": point["merged_point_count"],
                    "source_views": point["source_views"],
                }
                for point in cloud_points
            ]
        ).encode("utf-8")
    ).hexdigest()
    return {
        "voxel_size": canonical_float(voxel_size, 10),
        "representative_rule": REPRESENTATIVE_RULE,
        "reconstructed_point_count": len(members),
        "voxel_count": len(cloud_points),
        "cloud_digest": points_digest,
        "points": cloud_points,
    }
