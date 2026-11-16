from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .util import atomic_write_json


CANONICAL_BANDS = ["red", "green", "nir", "elevation"]


def _base_config(seed: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "seed": seed,
        "window_size": [15, 15],
        "stride": [8, 8],
        "context_overlap": 1,
        "feature_thresholds": {
            "ndvi": 0.30,
            "ndwi": 0.12,
            "edge_density": 0.055,
            "slope": 0.035,
            "brightness": 0.42,
            "elevation": 0.30,
        },
        "class_weights": {
            "1": {
                "bias": 0.36,
                "ndvi": 4.6,
                "ndwi": -2.8,
                "edge_density": -1.2,
                "slope": -0.8,
                "brightness": -0.7,
                "elevation": -0.7,
            },
            "2": {
                "bias": 0.18,
                "ndvi": -2.6,
                "ndwi": 5.2,
                "edge_density": -1.4,
                "slope": -0.8,
                "brightness": -1.2,
                "elevation": -0.4,
            },
            "3": {
                "bias": 0.05,
                "ndvi": -1.8,
                "ndwi": -1.2,
                "edge_density": 0.5,
                "slope": 0.8,
                "brightness": 2.8,
                "elevation": 1.1,
            },
            "4": {
                "bias": 0.10,
                "ndvi": -1.4,
                "ndwi": -1.0,
                "edge_density": 1.5,
                "slope": -0.6,
                "brightness": 1.3,
                "elevation": -0.5,
            },
        },
        "class_names": {
            "1": "farmland",
            "2": "water",
            "3": "building",
            "4": "road",
        },
        "fusion_strategy": "distance_weighted_vote",
        "min_connected_area": 10,
        "concurrency": 3,
        "recompute_budget": 2,
    }


def _make_world(seed: int, size: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    classes = np.ones((size, size), dtype=np.uint8)

    water = ((xx - 42.0) / 31.0) ** 2 + ((yy - 112.0) / 24.0) ** 2 <= 1.0
    water |= ((xx - 128.0) / 17.0) ** 2 + ((yy - 24.0) / 12.0) ** 2 <= 1.0
    classes[water] = 2

    building_mask = (
        (((xx >= 91) & (xx < 116)) | ((xx >= 122) & (xx < 143))) &
        (((yy >= 35) & (yy < 58)) | ((yy >= 67) & (yy < 89)))
    )
    classes[building_mask] = 3

    road_mask = ((yy >= 78) & (yy < 84)) | ((xx >= 78) & (xx < 85))
    road_mask &= ~water
    classes[road_mask] = 4

    colors = np.array(
        [
            [0.00, 0.00, 0.00, 0.00],
            [0.20, 0.39, 0.72, 0.20],
            [0.035, 0.25, 0.075, 0.07],
            [0.62, 0.63, 0.52, 0.58],
            [0.31, 0.32, 0.25, 0.19],
        ],
        dtype=np.float64,
    )
    bands = colors[classes].copy()

    texture = (
        0.035 * np.sin(xx * 0.43 + yy * 0.19)
        + 0.025 * np.cos(xx * 0.17 - yy * 0.37)
        + rng.normal(0.0, 0.012, size=(size, size))
    )
    bands[:, :, 0] += np.where(classes == 1, 0.25 * texture, 0.35 * texture)
    bands[:, :, 1] += np.where(classes == 1, texture, 0.30 * texture)
    bands[:, :, 2] += np.where(classes == 1, 1.25 * texture, 0.20 * texture)

    # Building footprints receive blocky airborne/DSM structure.
    block = (((xx // 5) + (yy // 6)) % 2 == 0).astype(np.float64)
    bands[:, :, 3] += np.where(building_mask, 0.08 * block, 0.0)
    bands[:, :, 3] += 0.025 * np.sin(xx * 0.09) * np.cos(yy * 0.11)

    # A narrow drainage channel improves water-edge separation.
    drainage = (np.abs(yy - (0.47 * xx + 18.0)) < 1.2) & (classes != 2) & (classes != 3)
    bands[drainage, 0] = 0.06
    bands[drainage, 1] = 0.28
    bands[drainage, 2] = 0.09
    bands[drainage, 3] = 0.10

    bands = np.clip(bands, 0.0, 1.0)
    bands_u8 = np.rint(bands * 255.0).astype(np.uint8)
    return classes, bands_u8


def _overlaps(a: list[int], b: list[int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _edit_sequence(seed: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "description": "Multi-batch edits exercising thresholds, weights, index versioning, overlays, invalid overlays and postprocessing.",
        "steps": [
            {
                "name": "threshold-tune",
                "config_patch": {
                    "feature_thresholds": {"ndvi": 0.33, "ndwi": 0.10},
                },
            },
            {
                "name": "road-weight-tune",
                "config_patch": {
                    "class_weights": {
                        "4": {
                            "bias": 0.08,
                            "ndvi": -1.0,
                            "ndwi": -1.0,
                            "edge_density": 1.8,
                            "slope": -0.5,
                            "brightness": 1.0,
                            "elevation": -0.7,
                        }
                    }
                },
            },
            {
                "name": "index-version-and-adjacency",
                "index_patch": {
                    "band_version": f"seed-{seed}-bands-v2",
                    "add_neighbors": [[0, 0, 0, 2], [0, 2, 2, 0]],
                },
            },
            {
                "name": "valid-local-band-patch",
                "overlay_ops": [
                    {
                        "id": "patch-center-water",
                        "tile": [1, 1],
                        "bbox": [8, 10, 31, 29],
                        "band_deltas": [-0.18, 0.08, -0.22, -0.05],
                    }
                ],
            },
            {
                "name": "invalid-patch-isolated",
                "invalid_overlay_ops": [
                    {
                        "id": "bad-bbox",
                        "tile": [0, 0],
                        "bbox": [58, 58, 72, 72],
                        "bands": [[[0.0]] * 14] * 4,
                    }
                ],
            },
            {
                "name": "recover-and-postprocess",
                "config_patch": {
                    "min_connected_area": 14,
                    "feature_thresholds": {"edge_density": 0.05},
                },
                "overlay_ops": [
                    {
                        "id": "patch-east-field",
                        "tile": [1, 2],
                        "bbox": [10, 8, 28, 24],
                        "band_deltas": [0.03, 0.10, 0.08, -0.02],
                    }
                ],
            },
        ],
    }


def generate_sample(output_dir: str | Path, seed: int = 20261117) -> dict[str, Any]:
    output = Path(output_dir).resolve()
    tiles_dir = output / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    refresh_manifest = output / "refresh.json"
    if refresh_manifest.exists():
        old = json.loads(refresh_manifest.read_text(encoding="utf-8"))
        for item in old.get("tiles", []):
            (output / item["path"]).unlink(missing_ok=True)

    tile_size = 64
    step = 48
    grid_rows = 3
    grid_cols = 3
    world_size = (grid_rows - 1) * step + tile_size
    classes, world = _make_world(seed, world_size)

    tiles: list[dict[str, Any]] = []
    for row in range(grid_rows):
        for col in range(grid_cols):
            x0 = col * step
            y0 = row * step
            x1 = x0 + tile_size
            y1 = y0 + tile_size
            bounds = [x0, y0, x1, y1]
            crop = world[y0:y1, x0:x1]
            rgba = np.empty((tile_size, tile_size, 4), dtype=np.uint8)
            rgba[:, :, :4] = crop
            rgba[:, :, 3] = crop[:, :, 3]
            image = Image.fromarray(rgba, mode="RGBA")
            relative = f"tiles/r{row}_c{col}.png"
            image.save(output / relative)
            tiles.append(
                {
                    "row": row,
                    "col": col,
                    "path": relative,
                    "bounds": bounds,
                    "bands": 4,
                    "band_version": f"seed-{seed}-bands-v1",
                    "band_order": CANONICAL_BANDS,
                    "neighbors": [],
                }
            )
    for tile in tiles:
        for other in tiles:
            if tile is other:
                continue
            if _overlaps(tile["bounds"], other["bounds"]):
                tile["neighbors"].append([other["row"], other["col"]])
        tile["neighbors"].sort()

    index = {
        "schema_version": 1,
        "index_version": f"sample-{seed}-v1",
        "band_version": f"seed-{seed}-bands-v1",
        "band_order": CANONICAL_BANDS,
        "tile_size": [tile_size, tile_size],
        "crs": "local-pixel",
        "tiles": tiles,
    }
    config = _base_config(seed)
    overlay = {"schema_version": 1, "patches": []}
    edits = _edit_sequence(seed)

    atomic_write_json(output / "tiles.json", index)
    atomic_write_json(output / "config.json", config)
    atomic_write_json(output / "overlay.json", overlay)
    atomic_write_json(output / "edits.json", edits)
    atomic_write_json(output / "ground_truth_classes.json", {
        "note": "Procedural class raster used only for sample inspection; the pipeline does not read this file.",
        "bounds": [0, 0, world_size, world_size],
        "classes": classes.tolist(),
    })
    refresh = {
        "seed": seed,
        "tile_size": tile_size,
        "step": step,
        "grid": [grid_rows, grid_cols],
        "tiles": tiles,
    }
    atomic_write_json(refresh_manifest, refresh)
    return {
        "output": str(output),
        "seed": seed,
        "tiles": len(tiles),
        "tile_size": tile_size,
        "global_size": world_size,
    }
