"""Deterministic three-schema geospatial feature sample generator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .jsonutil import to_jsonable


SCENE_ORIGIN = (500000.0, 3300000.0)
PIXEL_RESOLUTION = (10.0, 10.0)
TILE_ROWS = 3
TILE_COLS = 4
CHANNELS = 12

BANDS = [
    {
        "name": "low_spectral",
        "channels": [0, 1, 2, 3],
        "angle_base": 10000.0,
        "frequency_scale": 0.006,
        "axis": [1.0, 0.0],
    },
    {
        "name": "mid_spectral",
        "channels": [4, 5, 6, 7],
        "angle_base": 6000.0,
        "frequency_scale": 0.009,
        "axis": [0.0, 1.0],
    },
    {
        "name": "high_spectral",
        "channels": [8, 9, 10, 11],
        "angle_base": 2000.0,
        "frequency_scale": 0.004,
        "axis": [0.7071067811865476, 0.7071067811865476],
    },
]


def _tile_feature(seed: int, tile_index: int) -> list[Any]:
    rng = np.random.default_rng(seed * 100003 + tile_index * 97 + 11)
    values = rng.uniform(-1.0, 1.0, size=(TILE_ROWS, TILE_COLS, CHANNELS))
    return np.round(values, 10).tolist()


def _v1_block(
    seed: int, tile_key: str, origin: tuple[float, float], data: list[Any]
) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "block_id": f"v1-{tile_key}",
        "tile_set_id": f"scene-{seed}",
        "tile_key": tile_key,
        "origin_x": origin[0],
        "origin_y": origin[1],
        "origin_unit": "meters",
        "pixel_size": list(PIXEL_RESOLUTION),
        "local_index_origin": [0, 0],
        "shape": [TILE_ROWS, TILE_COLS, CHANNELS],
        "features": data,
        "channel_ranges": [
            {
                "name": band["name"],
                "channel_range": [band["channels"][0], band["channels"][-1] + 1],
            }
            for band in BANDS
        ],
        "frequency_bands": [
            {
                "name": band["name"],
                "angle_base": band["angle_base"],
                "frequency_scale": band["frequency_scale"],
                "frequency_axis": band["axis"],
            }
            for band in BANDS
        ],
    }


def _v2_block(
    seed: int, tile_key: str, origin: tuple[float, float], data: list[Any]
) -> dict[str, Any]:
    return {
        "schema_version": "v2",
        "block_id": f"v2-{tile_key}",
        "tile_set_id": f"scene-{seed}",
        "tile_key": tile_key,
        "geo_origin": [
            origin[0] / PIXEL_RESOLUTION[0],
            origin[1] / PIXEL_RESOLUTION[1],
        ],
        "coord_unit": "pixels",
        "resolution": list(PIXEL_RESOLUTION),
        "local_index_origin": [0, 0],
        "shape": [TILE_ROWS, TILE_COLS, CHANNELS],
        "feature_tensor": data,
        "channel_groups": [
            {
                "name": band["name"],
                "indices": list(band["channels"]),
                "rope": {
                    "base": band["angle_base"],
                    "freq_scale": band["frequency_scale"],
                    "axis": list(band["axis"]),
                },
            }
            for band in BANDS
        ],
    }


def _v3_block(
    seed: int, tile_key: str, origin: tuple[float, float], data: list[Any]
) -> dict[str, Any]:
    return {
        "schema_version": "v3",
        "block_id": f"v3-{tile_key}",
        "tile_set_id": f"scene-{seed}",
        "tile_key": tile_key,
        "geo_origin": {
            "east": origin[0],
            "north": origin[1],
            "unit": "meters",
        },
        "resolution_m": list(PIXEL_RESOLUTION),
        "local_index_origin": [0, 0],
        "shape": [TILE_ROWS, TILE_COLS, CHANNELS],
        "tensor": data,
        "bands": [
            {
                "name": band["name"],
                "channels": {
                    "start": band["channels"][0],
                    "end": band["channels"][-1] + 1,
                    "inclusive_end": False,
                },
                "rope_base": band["angle_base"],
                "frequency": {
                    "scale": band["frequency_scale"],
                    "axis": list(band["axis"]),
                },
            }
            for band in BANDS
        ],
    }


def generate_sample(seed: int = 42) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    tile_index = 0
    for row in range(2):
        for col in range(2):
            tile_key = f"tile-r{row}c{col}"
            origin = (
                SCENE_ORIGIN[0] + col * TILE_COLS * PIXEL_RESOLUTION[0],
                SCENE_ORIGIN[1] + row * TILE_ROWS * PIXEL_RESOLUTION[1],
            )
            data = _tile_feature(seed, tile_index)
            blocks.append(_v1_block(seed, tile_key, origin, data))
            blocks.append(_v2_block(seed, tile_key, origin, data))
            blocks.append(_v3_block(seed, tile_key, origin, data))
            tile_index += 1
    return to_jsonable(
        {
            "request_id": f"georope-seed-{seed}",
            "seed": seed,
            "tolerance": 1e-7,
            "phase_threshold": 1e-5,
            "pixel_anchor": "center",
            "blocks": blocks,
            "band_combinations": [
                {
                    "name": "low_only",
                    "bands": ["low_spectral"],
                },
                {
                    "name": "mid_high",
                    "bands": ["mid_spectral", "high_spectral"],
                },
                {
                    "name": "all_bands",
                    "bands": ["low_spectral", "mid_spectral", "high_spectral"],
                },
            ],
            "generator": {
                "name": "georope deterministic three-schema generator",
                "scene_origin_m": list(SCENE_ORIGIN),
                "pixel_resolution_m": list(PIXEL_RESOLUTION),
                "tile_shape": [TILE_ROWS, TILE_COLS, CHANNELS],
                "tile_grid": [2, 2],
                "feature_distribution": "uniform(-1,1), rounded to 10 decimals",
                "independent_reference": "georope.reference.verify_against_reference",
            },
        }
    )


def deterministic_json(payload: Any) -> str:
    return json.dumps(
        to_jsonable(payload),
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def write_sample(path: str | Path, seed: int = 42) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(deterministic_json(generate_sample(seed)), encoding="utf-8")
    return output


def generate_incremental_sample(seed: int = 42) -> dict[str, Any]:
    old_payload = generate_sample(seed)
    new_payload = json.loads(json.dumps(old_payload))
    changed_block = next(
        block
        for block in new_payload["blocks"]
        if block["block_id"] == "v1-tile-r0c0"
    )
    changed_block["frequency_bands"][0]["frequency_scale"] = 0.007
    changed_block["origin_x"] = changed_block["origin_x"] - PIXEL_RESOLUTION[0]
    return to_jsonable(
        {
            "old": old_payload,
            "new": new_payload,
            "description": (
                "v1 tile-r0c0 shifts left by one pixel and changes the low_spectral "
                "frequency scale; all other blocks are untouched."
            ),
        }
    )


def write_incremental_sample(path: str | Path, seed: int = 42) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        deterministic_json(generate_incremental_sample(seed)),
        encoding="utf-8",
    )
    return output
