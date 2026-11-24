"""Deterministic normalized soft-weight fusion."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np


def _sorted_tiles(tiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(tiles, key=lambda tile: str(tile["tile_id"]))


def fuse_weighted(
    arrays_by_tile: Dict[str, Dict[str, np.ndarray]],
    tiles: List[Dict[str, Any]],
    canvas_width: int,
    canvas_height: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fuse tiles in tile-id order; per-pixel contribution order is fixed."""

    ordered = _sorted_tiles(tiles)
    numerator = np.zeros((canvas_height, canvas_width), dtype=np.float64)
    denominator = np.zeros((canvas_height, canvas_width), dtype=np.float64)

    for tile in ordered:
        tile_id = str(tile["tile_id"])
        arrays = arrays_by_tile[tile_id]
        aligned = arrays["aligned"].astype(np.float64)
        weight = arrays["weight"].astype(np.float64)
        x0, y0 = int(tile["x"]), int(tile["y"])
        height, width = int(tile["height"]), int(tile["width"])
        numerator[y0:y0 + height, x0:x0 + width] += aligned * weight
        denominator[y0:y0 + height, x0:x0 + width] += weight

    safe_denominator = np.where(denominator > 1e-12, denominator, 1.0)
    fused_float = numerator / safe_denominator
    fused = np.clip(np.rint(fused_float), 0, 255).astype(np.uint8)
    return fused, fused_float, denominator


def fuse_serial_reference(
    arrays_by_tile: Dict[str, Dict[str, np.ndarray]],
    tiles: List[Dict[str, Any]],
    canvas_width: int,
    canvas_height: int,
) -> np.ndarray:
    """Explicit y/x row-order reference used by consistency tests."""

    ordered = _sorted_tiles(tiles)
    result = np.zeros((canvas_height, canvas_width), dtype=np.uint8)
    for y in range(canvas_height):
        for x in range(canvas_width):
            numerator = 0.0
            denominator = 0.0
            for tile in ordered:
                x0, y0 = int(tile["x"]), int(tile["y"])
                width, height = int(tile["width"]), int(tile["height"])
                if not (x0 <= x < x0 + width and y0 <= y < y0 + height):
                    continue
                local_x, local_y = x - x0, y - y0
                aligned = float(arrays_by_tile[tile["tile_id"]]["aligned"][local_y, local_x])
                weight = float(arrays_by_tile[tile["tile_id"]]["weight"][local_y, local_x])
                numerator += aligned * weight
                denominator += weight
            value = numerator / denominator if denominator > 1e-12 else 0.0
            result[y, x] = int(np.clip(np.rint(value), 0, 255))
    return result


def fusion_digest(image: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(np.ascontiguousarray(image).tobytes()).hexdigest()
