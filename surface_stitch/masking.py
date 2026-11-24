"""Mask geometry, generation conversion and two-generation arbitration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .errors import E_MASK_DOMAIN, E_MASK_SHAPE, fail


def soft_tile_weight(
    tile: Dict[str, Any], tiles: List[Dict[str, Any]], overlap: int = 128
) -> np.ndarray:
    """Deterministic linear feather weights in declared tile coordinates."""

    height = int(tile["height"])
    width = int(tile["width"])
    ys, xs = np.mgrid[0:height, 0:width]
    weight = np.ones((height, width), dtype=np.float64)
    tile_x = int(tile["x"])
    tile_y = int(tile["y"])

    for other in tiles:
        if other["tile_id"] == tile["tile_id"]:
            continue
        same_row = int(other["y"]) == tile_y
        same_column = int(other["x"]) == tile_x
        other_x = int(other["x"])
        other_y = int(other["y"])

        if same_row and other_x > tile_x:
            start = other_x - tile_x
            factor = 1.0 - np.clip((xs - start) / float(overlap), 0.0, 1.0)
            weight *= factor
        elif same_row and other_x < tile_x:
            factor = np.clip(xs / float(overlap), 0.0, 1.0)
            weight *= factor

        if same_column and other_y > tile_y:
            start = other_y - tile_y
            factor = 1.0 - np.clip((ys - start) / float(overlap), 0.0, 1.0)
            weight *= factor
        elif same_column and other_y < tile_y:
            factor = np.clip(ys / float(overlap), 0.0, 1.0)
            weight *= factor

    return np.clip(weight, 0.0, 1.0)


def hard_owner_masks(
    soft_weights: Dict[str, np.ndarray],
    tiles: List[Dict[str, Any]],
    canvas_width: int,
    canvas_height: int,
) -> Dict[str, np.ndarray]:
    """Convert soft maps to one-hot hard masks with lowest-id tie breaks."""

    ordered = sorted(tiles, key=lambda item: item["tile_id"])
    stack = np.zeros((len(ordered), canvas_height, canvas_width), dtype=np.float64)
    for index, tile in enumerate(ordered):
        weight = soft_weights[tile["tile_id"]]
        if weight.shape != (int(tile["height"]), int(tile["width"])):
            raise fail(E_MASK_SHAPE, "soft mask shape does not match tile")
        stack[
            index,
            int(tile["y"]):int(tile["y"]) + int(tile["height"]),
            int(tile["x"]):int(tile["x"]) + int(tile["width"]),
        ] = weight

    owner_index = np.argmax(stack, axis=0)
    masks = {
        tile["tile_id"]: np.zeros(
            (int(tile["height"]), int(tile["width"])), dtype=np.uint8
        )
        for tile in ordered
    }
    for index, tile in enumerate(ordered):
        owner_canvas = owner_index == index
        x0, y0 = int(tile["x"]), int(tile["y"])
        local_owner = owner_canvas[
            y0:y0 + int(tile["height"]), x0:x0 + int(tile["width"])
        ]
        masks[tile["tile_id"]][local_owner] = 255
    return masks


def validate_weight_array(
    values: Any,
    width: int,
    height: int,
    code: str = E_MASK_DOMAIN,
) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    expected = height * width
    if array.size != expected:
        raise fail(
            E_MASK_SHAPE,
            "mask pixel count does not match declared tile dimensions",
            expected=expected,
            got=int(array.size),
            width=width,
            height=height,
        )
    array = array.reshape(height, width)
    if not np.all(np.isfinite(array)):
        raise fail(code, "mask weights contain non-finite values")
    if np.any(array < 0.0) or np.any(array > 1.0):
        raise fail(code, "mask weights must be in [0, 1]")
    return array.astype(np.float32)


@dataclass
class MaskGenerationResult:
    weights_by_tile: Dict[str, np.ndarray]
    sources_by_tile: Dict[str, str]
    conflicts: List[Dict[str, Any]]
    tombstones: List[Dict[str, Any]]
    revisions: List[Dict[str, Any]]
    selected_records: List[Dict[str, Any]]


def _deduplicate_records(
    records: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    groups: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for record in records:
        groups.setdefault((int(record["generation"]), str(record["record_id"])), []).append(record)

    selected: List[Dict[str, Any]] = []
    revisions: List[Dict[str, Any]] = []
    for (generation, record_id), group in sorted(groups.items()):
        if len(group) > 1:
            revisions.append(
                {
                    "generation": generation,
                    "record_id": record_id,
                    "revision_count": len(group),
                    "effective_at_ms_values": [
                        int(item["effective_at_ms"]) for item in group
                    ],
                }
            )
        tombstones = [item for item in group if item.get("tombstone")]
        if tombstones:
            newest = max(tombstones, key=lambda item: int(item["effective_at_ms"]))
            chosen = dict(newest)
            chosen["_tombstone_priority"] = True
        else:
            chosen = max(
                group,
                key=lambda item: (
                    int(item["effective_at_ms"]),
                    str(item.get("source", "")),
                    int(item.get("source_index", 0)),
                ),
            )
        selected.append(chosen)
    return selected, revisions


def _place_generation_canvas(
    selected: List[Dict[str, Any]],
    tiles: List[Dict[str, Any]],
    canvas_width: int,
    canvas_height: int,
) -> Tuple[np.ndarray, Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    ordered = sorted(tiles, key=lambda item: item["tile_id"])
    tile_index = {tile["tile_id"]: index for index, tile in enumerate(ordered)}
    stack = np.zeros((len(ordered), canvas_height, canvas_width), dtype=np.float32)
    source_by_tile: Dict[str, Dict[str, Any]] = {}
    tombstones: List[Dict[str, Any]] = []

    for record in sorted(selected, key=lambda item: (item["generation"], item["tile_id"])):
        tile_id = str(record["tile_id"])
        if tile_id not in tile_index:
            continue
        if record.get("tombstone"):
            tombstones.append(
                {
                    "generation": record["generation"],
                    "record_id": record["record_id"],
                    "mask_id": record.get("mask_id"),
                    "tile_id": tile_id,
                    "effective_at_ms": record["effective_at_ms"],
                    "source": record.get("source"),
                }
            )
            continue
        weights = record.get("weights")
        if weights is None:
            continue
        tile = next(item for item in ordered if item["tile_id"] == tile_id)
        y0, x0 = int(tile["y"]), int(tile["x"])
        height, width = int(tile["height"]), int(tile["width"])
        stack[tile_index[tile_id], y0:y0 + height, x0:x0 + width] = weights
        # A newer selected record replaces the source for this tile.
        source_by_tile[tile_id] = record

    return stack, source_by_tile, tombstones


def arbitrate_masks(
    records: List[Dict[str, Any]],
    tiles: List[Dict[str, Any]],
    canvas_width: int,
    canvas_height: int,
) -> MaskGenerationResult:
    """Normalize masks and emit every cross-generation ownership conflict."""

    selected, revisions = _deduplicate_records(records)
    v1 = [record for record in selected if int(record["generation"]) == 1]
    v2 = [record for record in selected if int(record["generation"]) == 2]

    stack1, source1, tombs1 = _place_generation_canvas(
        v1, tiles, canvas_width, canvas_height
    )
    stack2, source2, tombs2 = _place_generation_canvas(
        v2, tiles, canvas_width, canvas_height
    )
    tombstones = tombs1 + tombs2
    ordered = sorted(tiles, key=lambda item: item["tile_id"])

    geometric_coverage = np.zeros((canvas_height, canvas_width), dtype=np.uint8)
    for tile in ordered:
        x0, y0 = int(tile["x"]), int(tile["y"])
        geometric_coverage[
            y0:y0 + int(tile["height"]), x0:x0 + int(tile["width"])
        ] += 1
    owner1 = np.argmax(stack1, axis=0)
    owner2 = np.argmax(stack2, axis=0)
    max1 = np.max(stack1, axis=0)
    max2 = np.max(stack2, axis=0)
    covered = geometric_coverage >= 2
    mismatch = covered & (owner1 != owner2) & (max1 >= 0.5) & (max2 >= 0.5)

    conflicts: List[Dict[str, Any]] = []
    mismatch_ys, mismatch_xs = np.where(mismatch)
    for y, x in zip(mismatch_ys.tolist(), mismatch_xs.tolist()):
        tile1 = ordered[int(owner1[y, x])]
        tile2 = ordered[int(owner2[y, x])]
        rec1 = source1.get(tile1["tile_id"], {})
        rec2 = source2.get(tile2["tile_id"], {})
        winner_gen = 2
        winner_record = rec2
        loser_record = rec1
        if int(rec1.get("generation", 1)) == int(rec2.get("generation", 2)):
            if int(rec1.get("effective_at_ms", 0)) > int(rec2.get("effective_at_ms", 0)):
                winner_gen = 1
                winner_record, loser_record = rec1, rec2
        elif int(rec1.get("generation", 1)) > int(rec2.get("generation", 2)):
            winner_gen = 1
            winner_record, loser_record = rec1, rec2

        winner_tile = str(winner_record.get("tile_id", tile2["tile_id"]))
        winner_tile_spec = next(item for item in ordered if item["tile_id"] == winner_tile)
        conflicts.append(
            {
                "canvas_xy": [int(x), int(y)],
                "tile_id": winner_tile,
                "pixel_xy": [
                    int(x - int(winner_tile_spec["x"])),
                    int(y - int(winner_tile_spec["y"])),
                ],
                "winner_tile_id": winner_tile,
                "loser_tile_id": str(loser_record.get("tile_id", tile1["tile_id"])),
                "v1": {
                    "source": rec1.get("source"),
                    "record_id": rec1.get("record_id"),
                    "mask_id": rec1.get("mask_id"),
                    "tile_id": tile1["tile_id"],
                    "pixel_xy": [
                        int(x - int(tile1["x"])),
                        int(y - int(tile1["y"])),
                    ],
                    "weight": float(stack1[int(owner1[y, x]), y, x]),
                    "effective_at_ms": rec1.get("effective_at_ms"),
                },
                "v2": {
                    "source": rec2.get("source"),
                    "record_id": rec2.get("record_id"),
                    "mask_id": rec2.get("mask_id"),
                    "tile_id": tile2["tile_id"],
                    "pixel_xy": [
                        int(x - int(tile2["x"])),
                        int(y - int(tile2["y"])),
                    ],
                    "weight": float(stack2[int(owner2[y, x]), y, x]),
                    "effective_at_ms": rec2.get("effective_at_ms"),
                },
                "inconsistency": "owner_mismatch",
                "adjudicated_generation": f"v{winner_gen}",
                "adjudicated_source": winner_record.get("source"),
                "reason": "higher generation rank, then effective_at_ms",
            }
        )

    weights_by_tile: Dict[str, np.ndarray] = {}
    sources_by_tile: Dict[str, str] = {}
    for index, tile in enumerate(ordered):
        tile_id = tile["tile_id"]
        y0, x0 = int(tile["y"]), int(tile["x"])
        height, width = int(tile["height"]), int(tile["width"])
        canvas_weight = stack2[index].copy() if tile_id in source2 else stack1[index].copy()
        source = source2.get(tile_id) or source1.get(tile_id)

        # Apply exceptional pixel-level decisions.  The generated sample's
        # conflicts are all won by v2, but this keeps adjudication general.
        for conflict in conflicts:
            cx, cy = conflict["canvas_xy"]
            if conflict["adjudicated_generation"] == "v1":
                if tile_id == conflict["winner_tile_id"]:
                    canvas_weight[cy, cx] = 1.0
                elif tile_id == conflict["loser_tile_id"]:
                    canvas_weight[cy, cx] = 0.0

        weights_by_tile[tile_id] = canvas_weight[
            y0:y0 + height, x0:x0 + width
        ].astype(np.float32)
        sources_by_tile[tile_id] = str(source.get("source", "unavailable")) if source else "unavailable"

    return MaskGenerationResult(
        weights_by_tile=weights_by_tile,
        sources_by_tile=sources_by_tile,
        conflicts=conflicts,
        tombstones=tombstones,
        revisions=revisions,
        selected_records=selected,
    )
