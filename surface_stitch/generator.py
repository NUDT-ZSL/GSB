"""Deterministic sample generator for the surface stitching pipeline."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
from PIL import Image

from .hashing import canonical_json_bytes, sha256_file
from .masking import hard_owner_masks, soft_tile_weight


SEED = 20261125
CANVAS_SIZE = 320
TILE_SIZE = 224
STRIDE = 96
MARGIN = 48
OVERLAP = TILE_SIZE - STRIDE
V1_TIME_ORIGIN_MS = 1764000000000
V2_BASE_MS = 1764200000000


TILE_SPECS: List[Dict[str, Any]] = [
    {
        "tile_id": "tile_00",
        "x": 0,
        "y": 0,
        "dx": 0.0,
        "dy": 0.0,
        "contrast": 1.00,
        "brightness": 0.0,
        "v1_seconds": 0.5,
    },
    {
        "tile_id": "tile_01",
        "x": STRIDE,
        "y": 0,
        "dx": 7.25,
        "dy": -6.75,
        "contrast": 0.86,
        "brightness": 18.0,
        "v1_seconds": 12.5,
    },
    {
        "tile_id": "tile_02",
        "x": 0,
        "y": STRIDE,
        "dx": -8.25,
        "dy": 5.75,
        "contrast": 1.14,
        "brightness": -17.0,
        "v1_seconds": 27.75,
    },
    {
        "tile_id": "tile_03",
        "x": STRIDE,
        "y": STRIDE,
        "dx": 4.75,
        "dy": 8.25,
        "contrast": 0.90,
        "brightness": 13.0,
        "v1_seconds": 41.125,
    },
]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _build_material() -> np.ndarray:
    rng = np.random.default_rng(SEED)
    material_size = CANVAS_SIZE + 2 * MARGIN
    y, x = np.mgrid[0:material_size, 0:material_size].astype(np.float64)
    # Coordinates are canvas coordinates; material index adds MARGIN.
    cx = x - MARGIN
    cy = y - MARGIN
    material = (
        126.0
        + 26.0 * np.sin(cx / 27.0 + 1.7 * np.sin(cy / 21.0))
        + 21.0 * np.cos(cy / 23.0 + cx / 35.0)
        + 10.0 * np.sin((cx + cy) / 13.0)
    )
    material += rng.normal(0.0, 15.0, material.shape)
    for amplitude in (8.0, 3.5):
        grain = rng.normal(0.0, amplitude, material.shape)
        for _ in range(2):
            grain = (
                grain * 0.5
                + np.roll(grain, 1, 0) * 0.125
                + np.roll(grain, -1, 0) * 0.125
                + np.roll(grain, 1, 1) * 0.125
                + np.roll(grain, -1, 1) * 0.125
            )
        material += grain
    return np.clip(material, 24.0, 232.0)


def _sample_tile(
    material: np.ndarray, spec: Dict[str, Any]
) -> np.ndarray:
    _yy, _xx = np.mgrid[0:TILE_SIZE, 0:TILE_SIZE]
    source_x = int(spec["x"]) + _xx.astype(np.float64) + float(spec["dx"])
    source_y = int(spec["y"]) + _yy.astype(np.float64) + float(spec["dy"])
    index_x = source_x + MARGIN
    index_y = source_y + MARGIN
    x0 = np.floor(index_x).astype(np.int64)
    y0 = np.floor(index_y).astype(np.int64)
    fx = index_x - x0
    fy = index_y - y0
    limit = material.shape[0] - 1

    def tap(ix: np.ndarray, iy: np.ndarray) -> np.ndarray:
        safe_x = np.clip(ix, 0, limit)
        safe_y = np.clip(iy, 0, limit)
        return material[safe_y, safe_x]

    value = (
        tap(x0, y0) * (1.0 - fx) * (1.0 - fy)
        + tap(x0 + 1, y0) * fx * (1.0 - fy)
        + tap(x0, y0 + 1) * (1.0 - fx) * fy
        + tap(x0 + 1, y0 + 1) * fx * fy
    )
    scale = float(spec["contrast"])
    offset = 128.0 * (1.0 - scale) + float(spec["brightness"])
    value = scale * value + offset
    rng = np.random.default_rng(SEED + int(spec["tile_id"].split("_")[1]))
    value += rng.normal(0.0, 1.25, value.shape)
    return np.clip(np.rint(value), 0, 255).astype(np.uint8), scale, offset


def _build_overlaps(tiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    overlaps: List[Dict[str, Any]] = []
    for left_index, first in enumerate(tiles):
        for second in tiles[left_index + 1:]:
            x0 = max(int(first["x"]), int(second["x"]))
            y0 = max(int(first["y"]), int(second["y"]))
            x1 = min(
                int(first["x"]) + int(first["width"]),
                int(second["x"]) + int(second["width"]),
            )
            y1 = min(
                int(first["y"]) + int(first["height"]),
                int(second["y"]) + int(second["height"]),
            )
            if x1 <= x0 or y1 <= y0:
                continue
            overlaps.append(
                {
                    "overlap_id": (
                        f"ov_{first['tile_id'][-2:]}_{second['tile_id'][-2:]}"
                    ),
                    "tile_ids": [first["tile_id"], second["tile_id"]],
                    "bbox": {
                        "x": x0,
                        "y": y0,
                        "width": x1 - x0,
                        "height": y1 - y0,
                    },
                }
            )
    return overlaps


def generate_sample(output_dir: Path) -> Dict[str, Any]:
    output_dir = Path(output_dir).resolve()
    (output_dir / "tiles").mkdir(parents=True, exist_ok=True)
    (output_dir / "descriptors" / "v2").mkdir(parents=True, exist_ok=True)

    material = _build_material()
    tiles: List[Dict[str, Any]] = []
    truth_tiles: List[Dict[str, Any]] = []
    images: Dict[str, np.ndarray] = {}
    radiometry_truth: Dict[str, Dict[str, float]] = {}

    for spec in TILE_SPECS:
        image, scale, offset = _sample_tile(material, spec)
        tile_id = spec["tile_id"]
        image_path = output_dir / "tiles" / f"{tile_id}.png"
        Image.fromarray(image, mode="L").save(image_path, format="PNG")
        images[tile_id] = image
        radiometry_truth[tile_id] = {"scale": scale, "offset": offset}
        tiles.append(
            {
                "tile_id": tile_id,
                "image": f"tiles/{tile_id}.png",
                "x": int(spec["x"]),
                "y": int(spec["y"]),
                "width": TILE_SIZE,
                "height": TILE_SIZE,
            }
        )
        integer_dx = int(np.rint(spec["dx"]))
        integer_dy = int(np.rint(spec["dy"]))
        truth_tiles.append(
            {
                "tile_id": tile_id,
                "offset_xy": [float(spec["dx"]), float(spec["dy"])],
                "integer_offset_xy": [integer_dx, integer_dy],
                "radiometry": {"scale": scale, "offset": offset},
            }
        )

    overlaps = _build_overlaps(tiles)
    soft_weights = {
        tile["tile_id"]: soft_tile_weight(tile, tiles, overlap=OVERLAP)
        for tile in tiles
    }
    hard_masks = hard_owner_masks(soft_weights, tiles, CANVAS_SIZE, CANVAS_SIZE)

    # A deliberate 4x3 ownership conflict near the v1/v2 seam.
    for global_y in range(40, 44):
        for global_x in range(154, 157):
            hard_masks["tile_00"][global_y, global_x] = 0
            hard_masks["tile_01"][global_y, global_x - STRIDE] = 255

    truth = {
        "schema": "surface-stitch-truth-v1",
        "seed": SEED,
        "coordinate_convention": "offset_xy maps destination nominal pixels to moving source at destination-offset",
        "canvas": {"width": CANVAS_SIZE, "height": CANVAS_SIZE},
        "tiles": truth_tiles,
    }
    _write_json(output_dir / "truth_offsets.json", truth)
    with (output_dir / "truth_offsets.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["tile_id", "dx", "dy", "integer_dx", "integer_dy", "scale", "offset"])
        for row in truth_tiles:
            writer.writerow(
                [
                    row["tile_id"],
                    row["offset_xy"][0],
                    row["offset_xy"][1],
                    row["integer_offset_xy"][0],
                    row["integer_offset_xy"][1],
                    row["radiometry"]["scale"],
                    row["radiometry"]["offset"],
                ]
            )

    v1_records: List[Dict[str, Any]] = []
    for index, spec in enumerate(TILE_SPECS):
        tile_id = spec["tile_id"]
        integer_offset = truth_tiles[index]["integer_offset_xy"]
        v1_records.append(
            {
                "record_id": f"rec_v1_{tile_id}",
                "mask_id": f"mask_{tile_id}",
                "tile_id": tile_id,
                "captured_at_rel_s": float(spec["v1_seconds"]),
                "integer_offset_xy": integer_offset,
                "flat_integer_offset": [integer_offset[0], integer_offset[1]],
                "mask": {
                    "kind": "hard",
                    "encoding": "flat_uint8_0_255_row_major",
                    "width": TILE_SIZE,
                    "height": TILE_SIZE,
                    "pixels": hard_masks[tile_id].astype(np.uint8).ravel().tolist(),
                },
            }
        )
    v1 = {
        "schema": "surface-stitch-mask-registration-v1",
        "generation": 1,
        "time_origin_ms": V1_TIME_ORIGIN_MS,
        "generated_at_rel_s": 0.0,
        "offset_order": "xy",
        "records": v1_records,
    }
    _write_json(output_dir / "descriptors" / "v1_registration.json", v1)

    shard_00_records: List[Dict[str, Any]] = []
    shard_01_records: List[Dict[str, Any]] = []
    for index, spec in enumerate(TILE_SPECS):
        tile_id = spec["tile_id"]
        target_list = shard_00_records if index < 2 else shard_01_records
        exact_offset = [float(spec["dx"]), float(spec["dy"])]
        record = {
            "record_id": f"rec_v2_{tile_id}",
            "mask_id": f"mask_{tile_id}",
            "tile_id": tile_id,
            "effective_at_ms": V2_BASE_MS + index * 60_000,
            "tombstone": False,
            "subpixel_offset_xy": exact_offset,
            "subpixel_offset_flat": [exact_offset[0], exact_offset[1]],
            "registration": {
                "offset_xy": exact_offset,
                "confidence_declared": 0.98,
                "max_error_tolerance_px": 0.2,
            },
            "soft_mask": {
                "kind": "soft",
                "encoding": "flat_float32_decimal_row_major",
                "width": TILE_SIZE,
                "height": TILE_SIZE,
                "weights": np.round(soft_weights[tile_id], 4).astype(float).ravel().tolist(),
            },
        }
        target_list.append(record)

    # Older revision of tile_00; loader must choose the newest same-id record.
    older_tile_00 = json.loads(json.dumps(shard_00_records[0]))
    older_tile_00["effective_at_ms"] = V2_BASE_MS - 120_000
    older_tile_00["soft_mask"]["weights"] = [0.85] * (TILE_SIZE * TILE_SIZE)
    shard_00_records.insert(0, older_tile_00)

    # An active manual override plus a newer tombstone for the same id.
    shard_00_records.append(
        {
            "record_id": "rec_v2_manual_conflict_001",
            "mask_id": "mask_manual_conflict_001",
            "tile_id": "tile_00",
            "effective_at_ms": V2_BASE_MS + 5_000,
            "tombstone": False,
            "subpixel_offset_xy": [0.0, 0.0],
            "soft_mask": {
                "kind": "soft",
                "encoding": "flat_float32_decimal_row_major",
                "width": TILE_SIZE,
                "height": TILE_SIZE,
                "weights": [0.5] * (TILE_SIZE * TILE_SIZE),
            },
        }
    )
    shard_00_records.append(
        {
            "record_id": "rec_v2_manual_conflict_001",
            "mask_id": "mask_manual_conflict_001",
            "tile_id": "tile_00",
            "effective_at_ms": V2_BASE_MS + 125_000,
            "tombstone": True,
        }
    )

    shards = [
        {
            "shard_id": "shard_00",
            "path": "shard_00.json",
            "records": shard_00_records,
        },
        {
            "shard_id": "shard_01",
            "path": "shard_01.json",
            "records": shard_01_records,
        },
    ]
    for shard in shards:
        _write_json(
            output_dir / "descriptors" / "v2" / shard["path"],
            {
                "schema": "surface-stitch-v2-shard",
                "shard_id": shard["shard_id"],
                "records": shard["records"],
            },
        )

    database_path = output_dir / "descriptors" / "v2" / "registry.sqlite3"
    if database_path.exists():
        database_path.unlink()
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE shards ("
            "shard_id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE records ("
            "record_id TEXT NOT NULL, revision_ms INTEGER NOT NULL, "
            "generation INTEGER NOT NULL, tile_id TEXT NOT NULL, "
            "shard_id TEXT NOT NULL, record_index INTEGER NOT NULL, "
            "mask_id TEXT, effective_at_ms INTEGER NOT NULL, "
            "tombstone INTEGER NOT NULL, offset_dx REAL, offset_dy REAL, "
            "PRIMARY KEY(record_id, revision_ms)"
            ")"
        )
        for shard in shards:
            shard_path = output_dir / "descriptors" / "v2" / shard["path"]
            digest = sha256_file(shard_path)
            connection.execute(
                "INSERT INTO shards VALUES (?, ?, ?)",
                (shard["shard_id"], shard["path"], digest),
            )
            for record_index, record in enumerate(shard["records"]):
                offset = record.get("subpixel_offset_xy", [0.0, 0.0])
                connection.execute(
                    "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["record_id"],
                        int(record["effective_at_ms"]),
                        2,
                        record["tile_id"],
                        shard["shard_id"],
                        record_index,
                        record.get("mask_id"),
                        int(record["effective_at_ms"]),
                        1 if record.get("tombstone") else 0,
                        float(offset[0]),
                        float(offset[1]),
                    ),
                )
        connection.commit()
    finally:
        connection.close()

    v2_manifest = {
        "schema": "surface-stitch-v2-manifest",
        "generation": 2,
        "absolute_timestamp_unit": "milliseconds",
        "database": "registry.sqlite3",
        "shards": [
            {
                "shard_id": shard["shard_id"],
                "path": shard["path"],
                "sha256": sha256_file(
                    output_dir / "descriptors" / "v2" / shard["path"]
                ),
            }
            for shard in shards
        ],
    }
    _write_json(output_dir / "descriptors" / "v2" / "v2_manifest.json", v2_manifest)

    project = {
        "schema": "surface-stitch-project-v1",
        "seed": SEED,
        "canvas": {"width": CANVAS_SIZE, "height": CANVAS_SIZE},
        "reference_tile_id": "tile_00",
        "subpixel_tolerance_px": 0.25,
        "integer_tolerance_px": 0,
        "min_confidence": 0.35,
        "max_shift_px": 32,
        "truth": "truth_offsets.json",
        "v1_descriptor": "descriptors/v1_registration.json",
        "v2_manifest": "descriptors/v2/v2_manifest.json",
        "resources": {
            "max_workers": 2,
            "max_inflight_pixels": 2_000_000,
            "max_spectrum_bytes": 64 * 1024 * 1024,
        },
        "tiles": tiles,
        "overlaps": overlaps,
    }
    _write_json(output_dir / "project.json", project)
    return project


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic samples")
    parser.add_argument(
        "--output",
        default="samples/generated",
        help="sample output directory",
    )
    args = parser.parse_args()
    project = generate_sample(Path(args.output))
    digest = hashlib.sha256(
        canonical_json_bytes({"seed": SEED, "tiles": project["tiles"]})
    ).hexdigest()
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(Path(args.output).resolve()),
                "tiles": len(project["tiles"]),
                "overlaps": len(project["overlaps"]),
                "sample_digest": digest,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
