from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

import numpy as np

from .io_utils import atomic_write_json


GLOBAL_SAMPLES = 128
TILE_SAMPLES = 32
WORLD_STEP = 0.25
ORIGIN_XZ = -16.0
SEED = 355


def deterministic_heightfield(seed: int = SEED) -> np.ndarray:
    axis = ORIGIN_XZ + np.arange(GLOBAL_SAMPLES, dtype=np.float64) * WORLD_STEP
    world_x, world_z = np.meshgrid(axis, axis)
    rng = np.random.default_rng(seed)

    heights = (
        1.7 * np.sin(world_x * 0.33 + 0.4) * np.cos(world_z * 0.27 - 0.7)
        + 1.05 * np.sin(world_x * 0.71 + 1.3) * np.sin(world_z * 0.63 + 0.2)
        + 0.48 * np.cos(world_x * 1.17 - world_z * 0.83)
    )
    heights += 3.0 * np.exp(
        -(((world_x - 3.5) / 3.4) ** 2 + ((world_z + 2.0) / 5.2) ** 2)
    )
    heights -= 2.1 * np.exp(
        -(((world_x + 4.2) / 4.1) ** 2 + ((world_z - 4.0) / 3.1) ** 2)
    )

    # Deterministic smooth coarse noise, bilinearly upsampled without SciPy.
    coarse_count = 9
    coarse = rng.normal(0.0, 0.55, size=(coarse_count, coarse_count))
    grid = np.linspace(0.0, coarse_count - 1.0, GLOBAL_SAMPLES)
    gx, gz = np.meshgrid(grid, grid)
    i0 = np.floor(gx).astype(int)
    j0 = np.floor(gz).astype(int)
    i1 = np.minimum(i0 + 1, coarse_count - 1)
    j1 = np.minimum(j0 + 1, coarse_count - 1)
    fx = gx - i0
    fz = gz - j0
    noise = (
        coarse[j0, i0] * (1 - fx) * (1 - fz)
        + coarse[j0, i1] * fx * (1 - fz)
        + coarse[j1, i0] * (1 - fx) * fz
        + coarse[j1, i1] * fx * fz
    )
    return heights + noise


def tile_manifest() -> list[Dict[str, Any]]:
    tiles = []
    blocks = GLOBAL_SAMPLES // TILE_SAMPLES
    for tile_row in range(blocks):
        for tile_col in range(blocks):
            row_start = tile_row * TILE_SAMPLES
            col_start = tile_col * TILE_SAMPLES
            tiles.append(
                {
                    "id": f"r{tile_row:02d}c{tile_col:02d}",
                    "file": f"tile_r{tile_row:02d}_c{tile_col:02d}.npy",
                    "row_start": row_start,
                    "col_start": col_start,
                    "rows": TILE_SAMPLES,
                    "cols": TILE_SAMPLES,
                }
            )
    return tiles


def scene_dict() -> Dict[str, Any]:
    max_world = ORIGIN_XZ + (GLOBAL_SAMPLES - 1) * WORLD_STEP
    return {
        "schema_version": "1.0",
        "scene_id": "deterministic_ridge_valley_355",
        "seed": SEED,
        "heightfield": {
            "directory": "heightfields",
            "origin": [ORIGIN_XZ, 0.0, ORIGIN_XZ],
            "world_scale": [WORLD_STEP, 1.0, WORLD_STEP],
            "global_rows": GLOBAL_SAMPLES,
            "global_cols": GLOBAL_SAMPLES,
            "world_bounds": {
                "min": [ORIGIN_XZ, -8.0, ORIGIN_XZ],
                "max": [max_world, 10.0, max_world],
            },
            "tiles": tile_manifest(),
        },
        "camera": {
            "position": [6.5, 25.0, -12.5],
            "target": [0.0, 1.0, 0.0],
            "up": [0.0, 1.0, 0.0],
            "fov_y_degrees": 42.0,
            "near": 0.1,
            "far": 90.0,
        },
        "lighting": {
            "direction": [-0.42, 0.86, -0.29],
            "ambient": 0.24,
            "intensity": 0.82,
        },
        "resolution": {
            "width": 256,
            "height": 192,
            "tile_width": 64,
            "tile_height": 64,
            "pixel_density": 8.0,
        },
        "ray_march": {
            "step_world": 0.22,
            "max_steps": 360,
            "refinement_steps": 8,
            "normal_radius_cells": 1.0,
        },
        "scheduler": {
            "max_workers": 4,
            "memory_budget_bytes": 8 * 1024 * 1024,
        },
        "output": {
            "grayscale": "terrain_gray.png",
            "depth": "terrain_depth.npy",
            "height": "terrain_height.npy",
            "height_depth": "terrain_height_depth.npz",
            "depth_preview": "terrain_depth.png",
            "normals": "terrain_normals.npy",
            "normal_preview": "terrain_normals.png",
            "report": "bake_report.json",
            "state_dir": "baker_state",
        },
    }


def edit_dict() -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "edit_id": "raise_ridge_lower_valley",
        "seed": SEED + 1,
        "regions": [
            {
                "name": "raise_ridge",
                "row_start": 49,
                "row_end_exclusive": 70,
                "col_start": 100,
                "col_end_exclusive": 118,
            },
            {
                "name": "lower_river_valley",
                "row_start": 58,
                "row_end_exclusive": 76,
                "col_start": 98,
                "col_end_exclusive": 114,
            },
        ],
        "operations": [
            {
                "type": "raise_ridge",
                "center": [109, 56],
                "amplitude": 3.2,
                "radius_cells": 7.0,
            },
            {
                "type": "lower_valley",
                "center": [105, 66],
                "amplitude": -2.4,
                "radius_cells": 7.0,
            },
        ],
    }


def _smooth_bump(rows: int, cols: int, center_col: int, center_row: int, radius: float) -> np.ndarray:
    row_axis = np.arange(rows)
    col_axis = np.arange(cols)
    grid_row, grid_col = np.meshgrid(row_axis, col_axis, indexing="ij")
    distance = np.sqrt((grid_col - center_col) ** 2 + (grid_row - center_row) ** 2)
    normalized = np.clip(distance / radius, 0.0, 1.0)
    return 0.5 + 0.5 * np.cos(np.pi * normalized)


def apply_edit_to_directory(sample_dir: str | Path, edit: Dict[str, Any] | None = None) -> Dict[str, Any]:
    sample_root = Path(sample_dir)
    height_dir = sample_root / "heightfields"
    edit = edit or edit_dict()
    scene_path = sample_root / "scene.initial.json"
    with open(scene_path, "r", encoding="utf-8-sig") as handle:
        scene = json.load(handle)

    global_height = np.full(
        (scene["heightfield"]["global_rows"], scene["heightfield"]["global_cols"]),
        np.nan,
        dtype=np.float64,
    )
    tile_arrays: Dict[str, np.ndarray] = {}
    for tile in scene["heightfield"]["tiles"]:
        path = height_dir / tile["file"]
        array = np.load(path, allow_pickle=False).astype(np.float64)
        tile_arrays[tile["id"]] = array
        r0, c0 = tile["row_start"], tile["col_start"]
        global_height[r0 : r0 + tile["rows"], c0 : c0 + tile["cols"]] = array

    changed_tile_ids = set()
    for operation in edit["operations"]:
        center_col, center_row = [int(v) for v in operation["center"]]
        radius = float(operation.get("radius_cells", 8.0))
        amplitude = float(operation["amplitude"])
        r0 = max(0, center_row - int(np.ceil(radius)) - 1)
        r1 = min(global_height.shape[0], center_row + int(np.ceil(radius)) + 2)
        c0 = max(0, center_col - int(np.ceil(radius)) - 1)
        c1 = min(global_height.shape[1], center_col + int(np.ceil(radius)) + 2)
        bump = _smooth_bump(r1 - r0, c1 - c0, center_col - c0, center_row - r0, radius)
        delta = amplitude * bump
        delta[bump <= 1e-12] = 0.0
        global_height[r0:r1, c0:c1] += delta

    for tile in scene["heightfield"]["tiles"]:
        r0, c0 = tile["row_start"], tile["col_start"]
        rows, cols = tile["rows"], tile["cols"]
        new_tile = global_height[r0 : r0 + rows, c0 : c0 + cols]
        tile_id = tile["id"]
        if not np.array_equal(new_tile, tile_arrays[tile_id]):
            np.save(height_dir / tile["file"], np.ascontiguousarray(new_tile, dtype="<f8"))
            changed_tile_ids.add(tile_id)

    edited_scene_path = sample_root / "scene.local_edit.json"
    edited_scene = dict(scene)
    edited_scene["scene_id"] = scene["scene_id"] + "_local_edit"
    edited_scene["applied_edit"] = edit["edit_id"]
    atomic_write_json(edited_scene_path, edited_scene)
    return {
        "changed_tile_ids": sorted(changed_tile_ids),
        "edited_scene": str(edited_scene_path),
        "edit_id": edit["edit_id"],
    }


APPLY_EDIT_SCRIPT = '''from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from terrain_baker.sample_generator import apply_edit_to_directory, edit_dict


if __name__ == "__main__":
    sample_dir = Path(__file__).resolve().parent
    edit_path = sample_dir / "edits" / "local_ridge_valley.json"
    if edit_path.exists():
        edit = json.loads(edit_path.read_text(encoding="utf-8"))
    else:
        edit = edit_dict()
    result = apply_edit_to_directory(sample_dir, edit)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
'''

APPLY_EDIT_BAT = """@echo off
setlocal
python "%~dp0apply_local_edit.py" %*
exit /b %ERRORLEVEL%
"""


def generate_samples(output_dir: str | Path = "samples", seed: int = SEED) -> Dict[str, str]:
    root = Path(output_dir)
    height_dir = root / "heightfields"
    edit_dir = root / "edits"
    height_dir.mkdir(parents=True, exist_ok=True)
    edit_dir.mkdir(parents=True, exist_ok=True)

    heights = deterministic_heightfield(seed)
    tiles = tile_manifest()
    for tile in tiles:
        r0 = tile["row_start"]
        c0 = tile["col_start"]
        array = heights[r0 : r0 + tile["rows"], c0 : c0 + tile["cols"]]
        np.save(height_dir / tile["file"], np.ascontiguousarray(array, dtype="<f8"))

    scene = scene_dict()
    scene["seed"] = seed
    atomic_write_json(root / "scene.initial.json", scene)
    edit = edit_dict()
    atomic_write_json(edit_dir / "local_ridge_valley.json", edit)
    (root / "apply_local_edit.py").write_text(APPLY_EDIT_SCRIPT, encoding="utf-8", newline="\n")
    (root / "apply_local_edit.bat").write_text(APPLY_EDIT_BAT, encoding="utf-8", newline="\r\n")
    readme = root / "README.md"
    readme.write_text(
        "# Deterministic sample\\n\\n"
        "Run `python apply_local_edit.py` to sculpt the ridge and valley, then bake "
        "`scene.local_edit.json` with `--edit edits/local_ridge_valley.json`.\\n",
        encoding="utf-8",
    )
    return {
        "sample_dir": str(root.resolve()),
        "scene": str((root / "scene.initial.json").resolve()),
        "edit": str((edit_dir / "local_ridge_valley.json").resolve()),
        "edit_script": str((root / "apply_local_edit.py").resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic terrain-baker samples")
    parser.add_argument("--output", default="samples", help="sample output directory")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    result = generate_samples(args.output, args.seed)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
