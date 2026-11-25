from __future__ import annotations

import math
from typing import Any, Iterator

import numpy as np

from .contracts import output_spatial_shape
from .hashing import sha256_json


def iter_tile_ranges(
    out_h: int, out_w: int, block_h: int, block_w: int
) -> Iterator[dict[str, int | str]]:
    tile_y = 0
    for y0 in range(0, out_h, block_h):
        y1 = min(out_h, y0 + block_h)
        tile_x = 0
        for x0 in range(0, out_w, block_w):
            x1 = min(out_w, x0 + block_w)
            yield {
                "tile_id": f"t{tile_y:03d}_{tile_x:03d}",
                "y0": y0,
                "y1": y1,
                "x0": x0,
                "x1": x1,
            }
            tile_x += 1
        tile_y += 1


def resolve_indices(
    indices: np.ndarray, size: int, boundary: str
) -> tuple[np.ndarray, np.ndarray]:
    index_array = np.asarray(indices, dtype=np.int64)
    in_bounds = (index_array >= 0) & (index_array < size)

    if boundary == "zero":
        mapped = np.where(in_bounds, index_array, 0).astype(np.int64)
        return mapped, in_bounds

    if boundary == "replicate":
        mapped = np.clip(index_array, 0, size - 1).astype(np.int64)
        return mapped, np.ones_like(in_bounds, dtype=bool)

    if boundary == "reflect":
        if size == 1:
            return np.zeros_like(index_array, dtype=np.int64), np.ones_like(in_bounds, dtype=bool)
        period = 2 * (size - 1)
        wrapped = np.mod(index_array, period)
        mapped = np.where(wrapped >= size, period - wrapped, wrapped).astype(np.int64)
        return mapped, np.ones_like(in_bounds, dtype=bool)

    raise ValueError(f"unsupported boundary mode: {boundary}")


def required_input_span(
    task: dict[str, Any],
    y0: int,
    y1: int,
    x0: int,
    x1: int,
) -> tuple[int, int, int, int]:
    if task["op_kind"] == "conv2d":
        kernel_h, kernel_w = task["kernel_shape"]
        stride_h, stride_w = task["stride"]
        pad_h, pad_w = task["padding"]
        iy_min = y0 * stride_h - pad_h
        iy_max = (y1 - 1) * stride_h - pad_h + kernel_h - 1
        ix_min = x0 * stride_w - pad_w
        ix_max = (x1 - 1) * stride_w - pad_w + kernel_w - 1
        return iy_min, iy_max, ix_min, ix_max

    scale_h, scale_w = task["scale"]
    first_y = y0 / scale_h
    last_y = (y1 - 1) / scale_h
    first_x = x0 / scale_w
    last_x = (x1 - 1) / scale_w
    mode = task["interp_mode"]

    if mode == "nearest":
        iy_min = math.floor(first_y + 0.5)
        iy_max = math.floor(last_y + 0.5)
        ix_min = math.floor(first_x + 0.5)
        ix_max = math.floor(last_x + 0.5)
    elif mode == "bilinear":
        iy_min = math.floor(first_y)
        iy_max = math.floor(last_y) + 1
        ix_min = math.floor(first_x)
        ix_max = math.floor(last_x) + 1
    else:
        iy_min = math.floor(first_y) - 1
        iy_max = math.floor(last_y) + 2
        ix_min = math.floor(first_x) - 1
        ix_max = math.floor(last_x) + 2
    return iy_min, iy_max, ix_min, ix_max


def _candidate_score(
    task: dict[str, Any], block_h: int, block_w: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    out_h, out_w = output_spatial_shape(task)
    _, input_channels, in_h, in_w = task["input_shape"]
    ranges = list(iter_tile_ranges(out_h, out_w, block_h, block_w))
    waste = 0
    oob_units = 0
    tiles: list[dict[str, Any]] = []

    for raw_tile in ranges:
        y0 = int(raw_tile["y0"])
        y1 = int(raw_tile["y1"])
        x0 = int(raw_tile["x0"])
        x1 = int(raw_tile["x1"])
        tile_h = y1 - y0
        tile_w = x1 - x0
        waste += block_h * block_w - tile_h * tile_w
        iy_min, iy_max, ix_min, ix_max = required_input_span(task, y0, y1, x0, x1)
        oob_y = max(0, -iy_min) + max(0, iy_max - (in_h - 1))
        oob_x = max(0, -ix_min) + max(0, ix_max - (in_w - 1))
        oob_units += oob_y + oob_x
        tile = dict(raw_tile)
        tile["input_span"] = [iy_min, iy_max, ix_min, ix_max]
        tiles.append(tile)

    area = block_h * block_w
    max_elements = int(task["block_policy"]["max_tile_elements"])
    metrics = {
        "block_size": [block_h, block_w],
        "area": area,
        "tile_count": len(ranges),
        "edge_waste_cells": waste,
        "out_of_bounds_index_units": oob_units,
        "fits_element_budget": area * max(1, input_channels) <= max_elements,
    }
    return metrics, tiles


def _choose_candidate(scores: list[dict[str, Any]], selection: str) -> tuple[int, str]:
    if selection == "largest_fit":
        fitting = [item for item in scores if item["fits_element_budget"]]
        pool = fitting or scores
        index = max(range(len(pool)), key=lambda i: (pool[i]["area"], -i))
        selected = scores.index(pool[index]) if pool is not scores else index
        return selected, "largest candidate area within max_tile_elements"

    if selection == "min_waste":
        index = min(
            range(len(scores)),
            key=lambda i: (
                scores[i]["edge_waste_cells"],
                scores[i]["tile_count"],
                -scores[i]["area"],
                i,
            ),
        )
        return index, "minimum edge waste, then minimum tile count"

    if selection == "min_oob":
        index = min(
            range(len(scores)),
            key=lambda i: (
                scores[i]["out_of_bounds_index_units"],
                scores[i]["edge_waste_cells"],
                scores[i]["tile_count"],
                i,
            ),
        )
        return index, "minimum out-of-bounds index units, then edge waste"

    index = min(
        range(len(scores)),
        key=lambda i: (
            not scores[i]["fits_element_budget"],
            scores[i]["edge_waste_cells"] + scores[i]["out_of_bounds_index_units"],
            scores[i]["tile_count"],
            -scores[i]["area"],
            i,
        ),
    )
    return index, "balanced edge waste, boundary access and tile count"


def build_plan(task: dict[str, Any]) -> dict[str, Any]:
    policy = task["block_policy"]
    scores: list[dict[str, Any]] = []
    tiles_by_candidate: list[list[dict[str, Any]]] = []
    for block_h, block_w in policy["candidates"]:
        metrics, tiles = _candidate_score(task, int(block_h), int(block_w))
        scores.append(metrics)
        tiles_by_candidate.append(tiles)

    selected_index, reason = _choose_candidate(scores, policy["selection"])
    selected = scores[selected_index]
    plan: dict[str, Any] = {
        "schema": "opverify-plan-v1",
        "job_id": task["job_id"],
        "output_shape": task["output_shape"],
        "spatial_shape": list(output_spatial_shape(task)),
        "boundary": policy["boundary"],
        "selection": policy["selection"],
        "selection_reason": reason,
        "selected_block_size": selected["block_size"],
        "candidate_scores": scores,
        "tiles": tiles_by_candidate[selected_index],
    }
    plan["plan_digest"] = sha256_json({k: v for k, v in plan.items() if k != "plan_digest"})
    return plan
