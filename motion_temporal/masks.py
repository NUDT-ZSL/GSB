"""Deterministic sparse motion-mask construction and summaries."""

from __future__ import annotations

import math
from typing import Iterable

from .canonical import checksum, r6
from .models import Box, ClipSpec


def sparse_mask_for_box(
    spec: ClipSpec,
    object_id: int,
    frame_index: int,
    box: Box,
    *,
    source_salt: int = 0,
) -> tuple[tuple[int, int], ...]:
    x0 = max(0, int(math.floor(box[0])))
    y0 = max(0, int(math.floor(box[1])))
    x1 = min(spec.canvas_width - 1, int(math.ceil(box[2])))
    y1 = min(spec.canvas_height - 1, int(math.ceil(box[3])))
    points: list[tuple[int, int]] = []
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if (x * 3 + y * 5 + object_id * 7 + frame_index + source_salt) % 11 == 0:
                points.append((y, x))
    if not points and x0 <= x1 and y0 <= y1:
        points.append((y0, x0))
    return tuple(points)


def mask_summary(
    shape: tuple[int, int],
    coords: Iterable[tuple[int, int]],
) -> dict[str, object]:
    point_list = [[int(y), int(x)] for y, x in coords]
    if point_list:
        ys = [point[0] for point in point_list]
        xs = [point[1] for point in point_list]
        bounds = [min(xs), min(ys), max(xs), max(ys)]
    else:
        bounds = [0, 0, 0, 0]
    return {
        "shape": [int(shape[0]), int(shape[1])],
        "pixel_count": len(point_list),
        "bounds": [int(value) for value in bounds],
        "checksum": checksum(point_list),
    }


def make_predicted_mask(
    spec: ClipSpec,
    object_id: int,
    frame_index: int,
    box: Box,
    source: str,
) -> dict[str, object]:
    salt = 0 if source == "observed" else 5
    coords = sparse_mask_for_box(spec, object_id, frame_index, box, source_salt=salt)
    summary = mask_summary((spec.canvas_height, spec.canvas_width), coords)
    summary["coords"] = [[int(y), int(x)] for y, x in coords]
    summary["status"] = "observed_mask" if source == "observed" else "extrapolated_mask"
    summary["confidence"] = 1.0 if source == "observed" else r6(0.5)
    return summary

