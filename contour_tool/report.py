"""Turn validated scenes into the result payload consumed by the JSON report."""

from __future__ import annotations

import math
from typing import Any

from .contour import LevelContours, QueryHit, locate
from .scene import Scene


def _json_number(value: Any) -> Any:
    """Keep only finite numbers; anything else becomes null (valid JSON)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def _clean_query(scene: Scene, index: int) -> dict[str, Any]:
    raw = scene.raw_queries[index]
    entry: dict[str, Any] = {"index": index}
    entry["x"] = _json_number(raw.get("x"))
    entry["y"] = _json_number(raw.get("y"))
    label = raw.get("label")
    if isinstance(label, str):
        entry["label"] = label
    return entry


def build_result(
    scene: Scene,
    contours: dict[float, LevelContours],
    affected_levels: tuple[float, ...],
    warnings: list[str],
    queries: list[QueryHit] | None = None,
) -> dict[str, Any]:
    hits = queries if queries is not None else locate(scene, contours)
    data_min, data_max = scene.data_min, scene.data_max

    levels_payload = []
    for level in scene.levels:
        contour = contours[level]
        in_range = (
            data_min is not None
            and data_max is not None
            and data_min <= level <= data_max
        )
        entries = [
            {
                "start": [round(a[0], 6), round(a[1], 6)],
                "end": [round(b[0], 6), round(b[1], 6)],
            }
            for a, b in contour.segments
        ]
        levels_payload.append(
            {
                "level": level,
                "in_data_range": in_range,
                "empty": contour.empty,
                "segment_count": len(entries),
                "segments": entries,
            }
        )

    query_payload = []
    for index, hit in enumerate(hits):
        entry = _clean_query(scene, index)
        entry["status"] = hit.status
        entry["level"] = hit.level
        entry["nearest"] = (
            [round(hit.nearest[0], 6), round(hit.nearest[1], 6)]
            if hit.nearest is not None
            else None
        )
        entry["distance"] = round(hit.distance, 6) if hit.distance is not None else None
        if hit.reason is not None:
            entry["reason"] = hit.reason
        if hit.nearest is not None:
            px, py = scene.world_to_pixel(*hit.nearest)
            entry["pixel"] = [round(px, 3), round(py, 3)]
        else:
            entry["pixel"] = None
        query_payload.append(entry)

    return {
        "schema": "grid-contour-result/1",
        "grid": {"width": scene.grid.width, "height": scene.grid.height},
        "world": {
            "min_x": scene.world.min_x,
            "min_y": scene.world.min_y,
            "max_x": scene.world.max_x,
            "max_y": scene.world.max_y,
        },
        "canvas": {"width": scene.canvas.width, "height": scene.canvas.height},
        "data_range": (
            {"min": data_min, "max": data_max} if data_min is not None else None
        ),
        "levels": levels_payload,
        "affected_levels": list(affected_levels),
        "queries": query_payload,
        "summary": {
            "level_count": len(levels_payload),
            "empty_level_count": sum(1 for item in levels_payload if item["empty"]),
            "query_count": len(query_payload),
            "hit_count": sum(1 for item in query_payload if item["status"] == "hit"),
            "miss_count": sum(1 for item in query_payload if item["status"] == "miss"),
            "skipped_count": sum(1 for item in query_payload if item["status"] == "skipped"),
        },
        "warnings": warnings,
    }
