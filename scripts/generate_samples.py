"""Generate deterministic local samples with a fixed seed.

Running this script creates the full sample bundle under ``samples/`` with no
network access: a terrain grid, a render task description, an append-only
edit manifest and an exception-injection manifest.  The default camera looks
straight down at the terrain so the whole grid is covered and every output
pixel hits the surface.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


SEED = 20261125


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")


def build_terrain() -> dict:
    rng = np.random.default_rng(SEED)
    rows_nx, rows_ny = 48, 36
    # Layered deterministic hills plus a small bounded noise term.
    grid_x, grid_y = np.meshgrid(
        np.arange(rows_nx, dtype=np.float64),
        np.arange(rows_ny, dtype=np.float64))
    hills = (
        18 * np.sin(grid_x / 6.0)
        + 14 * np.cos(grid_y / 5.0)
        + 8 * np.sin((grid_x + grid_y) / 9.0))
    noise = rng.integers(-3, 4, size=(rows_ny, rows_nx))
    heights = np.rint(hills).astype(np.int64) + noise
    return {
        "rows_nx": rows_nx,
        "rows_ny": rows_ny,
        "scale_x": 100.0,   # millimetres per grid cell
        "scale_y": 100.0,
        "scale_z": 50.0,    # millimetres per integer height unit
        "heights": heights.tolist(),
    }


def build_task() -> dict:
    return {
        "terrain_file": "terrain.json",
        "lights": [
            {"direction": [-0.4, -0.3, -0.86]},
            {"direction": [0.5, 0.2, -0.84]},
        ],
        "ambient": 0.15,
        "camera": {
            # Camera above the grid looking straight down (-z).  The image
            # plane matches the terrain footprint exactly (cells span
            # (nx-1)*sx by (ny-1)*sy), so every output pixel hits a surface.
            "position": [2350.0, 1750.0, 3000.0],
            "target": [2350.0, 1750.0, 0.0],
            "up": [0.0, 1.0, 0.0],
            "width": 4700.0,
            "height": 3500.0,
            "near": 100.0,
            "far": 6000.0,
        },
        "width_px": 96,
        "height_px": 72,
        "samples": 4,
        "tiles": {"tile_width": 32, "tile_height": 24},
        "max_concurrency": 3,
        "time_budget_ms": 120.0,
        "memory_budget_bytes": 600_000,
        "ray_step_mm": 25.0,
        "state_dir": "state",
    }


def build_edits() -> dict:
    return {
        "edits": [
            {"batch": 1, "op": "raise", "x0": 4, "y0": 3,
             "x1": 10, "y1": 8, "amount": 6},
            {"batch": 1, "op": "lower", "x0": 30, "y0": 20,
             "x1": 38, "y1": 27, "amount": 5},
            {"batch": 2, "op": "smooth", "x0": 12, "y0": 10,
             "x1": 20, "y1": 16, "amount": 0},
            {"batch": 2, "op": "raise", "x0": 40, "y0": 4,
             "x1": 46, "y1": 9, "amount": 4},
            {"batch": 3, "op": "lower", "x0": 2, "y0": 28,
             "x1": 8, "y1": 34, "amount": 3},
        ]
    }


def build_injections() -> dict:
    # Tile ids follow r{row:04d}_c{col:04d}; the grid is 3 columns x 3 rows.
    return {
        "height_count_mismatch": [
            {"tile": "r0000_c0002", "declared": 9999},
        ],
        "corrupt_digest": [
            {"tile": "r0002_c0002"},
        ],
        "tile_not_divisible": {
            "width_px": 96,
            "tile_width": 30,
            "reason": "96 is not divisible by 30",
        },
        "out_of_bounds_edit": [
            {"batch": 9, "op": "raise", "x0": 40, "y0": 30,
             "x1": 55, "y1": 40, "amount": 2},
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    out = Path(args.out)
    write_json(out / "terrain.json", build_terrain())
    write_json(out / "task.json", build_task())
    write_json(out / "edits.json", build_edits())
    write_json(out / "injections.json", build_injections())
    print(f"samples written to {out}")


if __name__ == "__main__":
    main()

