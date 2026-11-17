"""Projection, quantized depth-map fusion, and deterministic smoothing."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .canonical import canonical_float
from .geometry import project_point
from .io_views import Camera


def generate_depth_map(
    *,
    view_index: int,
    camera: Camera,
    source_points: list[dict[str, Any]],
    depth_params: dict[str, Any],
    triangulation_params: dict[str, Any],
    image_size: tuple[int, int],
) -> dict[str, Any]:
    width = int(depth_params["width"])
    height = int(depth_params["height"])
    levels = int(depth_params["quantization_levels"])
    minimum_depth = float(triangulation_params["min_depth"])
    maximum_depth = float(triangulation_params["max_depth"])
    depth_range = maximum_depth - minimum_depth
    image_width, image_height = image_size

    contributions: list[dict[str, Any]] = []
    for point in source_points:
        point_xyz = np.asarray(point["xyz"], dtype=np.float64)
        try:
            projected, depth = project_point(camera, point_xyz)
        except Exception:
            continue
        if depth < minimum_depth or depth > maximum_depth:
            continue
        u = float(projected[0])
        v = float(projected[1])
        if u < 0.0 or v < 0.0 or u >= image_width or v >= image_height:
            continue
        cell_x = min(width - 1, max(0, int(math.floor(u * width / image_width))))
        cell_y = min(height - 1, max(0, int(math.floor(v * height / image_height))))
        contributions.append(
            {
                "cell": (cell_y, cell_x),
                "depth": depth,
                "point_id": str(point["point_id"]),
            }
        )

    # A cell retains the closest projected point. Ties are broken by point id,
    # never by thread completion order.
    selected_by_cell: dict[tuple[int, int], dict[str, Any]] = {}
    for contribution in contributions:
        cell = contribution["cell"]
        current = selected_by_cell.get(cell)
        if current is None or (
            contribution["depth"], contribution["point_id"]
        ) < (current["depth"], current["point_id"]):
            selected_by_cell[cell] = contribution

    raw_map = np.zeros((height, width), dtype=np.float64)
    point_ids: list[str] = []
    for cell, contribution in sorted(selected_by_cell.items()):
        cell_y, cell_x = cell
        raw_map[cell_y, cell_x] = float(contribution["depth"])
        point_ids.append(str(contribution["point_id"]))

    quantized = np.zeros((height, width), dtype=np.int64)
    nonzero = raw_map > 0.0
    if np.any(nonzero):
        normalized = (raw_map[nonzero] - minimum_depth) / depth_range
        quantized_values = 1 + np.rint(normalized * (levels - 2)).astype(np.int64)
        quantized[nonzero] = np.clip(quantized_values, 1, levels - 1)

    smoothing_iterations = int(depth_params["smoothing_iterations"])
    smoothing_weight = float(depth_params["smoothing_weight"])
    smoothed = quantized.astype(np.float64)
    for _ in range(smoothing_iterations):
        next_map = smoothed.copy()
        for cell_y in range(height):
            for cell_x in range(width):
                center = smoothed[cell_y, cell_x]
                if center <= 0.0:
                    continue
                neighbors: list[float] = []
                if cell_y > 0 and smoothed[cell_y - 1, cell_x] > 0.0:
                    neighbors.append(float(smoothed[cell_y - 1, cell_x]))
                if cell_y + 1 < height and smoothed[cell_y + 1, cell_x] > 0.0:
                    neighbors.append(float(smoothed[cell_y + 1, cell_x]))
                if cell_x > 0 and smoothed[cell_y, cell_x - 1] > 0.0:
                    neighbors.append(float(smoothed[cell_y, cell_x - 1]))
                if cell_x + 1 < width and smoothed[cell_y, cell_x + 1] > 0.0:
                    neighbors.append(float(smoothed[cell_y, cell_x + 1]))
                if neighbors:
                    neighbor_mean = sum(neighbors) / len(neighbors)
                    next_map[cell_y, cell_x] = (
                        (1.0 - smoothing_weight) * center + smoothing_weight * neighbor_mean
                    )
        smoothed = next_map

    final_map = np.zeros((height, width), dtype=np.int64)
    final_nonzero = smoothed > 0.0
    final_map[final_nonzero] = np.clip(
        np.rint(smoothed[final_nonzero]).astype(np.int64), 1, levels - 1
    )
    nonzero_cells = int(np.count_nonzero(final_map))
    return {
        "view_index": view_index,
        "resolution": [width, height],
        "quantization": {
            "levels": levels,
            "background_value": 0,
            "value_range": [1, levels - 1],
            "formula": "1 + round((depth-min_depth)/(max_depth-min_depth)*(levels-2))",
        },
        "smoothing": {
            "iterations": smoothing_iterations,
            "weight": canonical_float(smoothing_weight, 10),
            "neighborhood": "4-neighbor-mean-of-valid-cells",
        },
        "projected_point_count": len(contributions),
        "unique_depth_cells": len(selected_by_cell),
        "nonzero_cells": nonzero_cells,
        "contributing_point_ids": point_ids,
        "depth_map": final_map.tolist(),
    }
