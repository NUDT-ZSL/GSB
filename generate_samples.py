"""Fixed-seed deterministic sample generator.

Run once to materialize the level description, render task and a small
reference manifest.  Only the Python standard library is used; the output is
fully reproducible from the embedded seed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random

SEED = 20261125


def _non_overlapping_room(rng, occupied, grid_width, grid_height):
    for _attempt in range(500):
        width = rng.randint(6, 9)
        height = rng.randint(5, 8)
        col = rng.randint(1, grid_width - width - 1)
        row = rng.randint(1, grid_height - height - 1)
        candidate = {
            (c, r)
            for c in range(col, col + width)
            for r in range(row, row + height)
        }
        if candidate.isdisjoint(occupied):
            return col, row, width, height, candidate
    raise RuntimeError("could not place a non-overlapping room")


def generate_level() -> dict:
    rng = random.Random(SEED)
    grid_width, grid_height = 48, 36
    occupied: set[tuple[int, int]] = set()
    rooms: list[dict] = []
    centers: list[tuple[int, int]] = []
    for room_id in range(6):
        col, row, width, height, cells = _non_overlapping_room(
            rng, occupied, grid_width, grid_height
        )
        occupied.update(cells)
        rooms.append(
            {
                "id": room_id,
                "cells": sorted(list(cell) for cell in cells),
            }
        )
        centers.append((col + width // 2, row + height // 2))

    corridors: set[tuple[int, int]] = set()

    def carve_segment(start, end):
        col, row = start
        target_col, target_row = end
        if rng.random() < 0.5:
            while col != target_col:
                col += 1 if target_col > col else -1
                corridors.add((col, row))
            while row != target_row:
                row += 1 if target_row > row else -1
                corridors.add((col, row))
        else:
            while row != target_row:
                row += 1 if target_row > row else -1
                corridors.add((col, row))
            while col != target_col:
                col += 1 if target_col > col else -1
                corridors.add((col, row))

    for index in range(1, len(centers)):
        carve_segment(centers[index - 1], centers[index])
    carve_segment(centers[-1], centers[0])
    corridors = {cell for cell in corridors if cell not in occupied}

    walkable = occupied | corridors
    doors = _place_doors(rng, walkable, grid_width, grid_height)
    level = {
        "width": grid_width,
        "height": grid_height,
        "rooms": rooms,
        "corridors": sorted(list(cell) for cell in corridors),
        "doors": sorted(list(cell) for cell in doors),
        "wall_thickness": 2,
        "light_radius": 14.0,
        "palette": [
            [0.0, 60],
            [0.25, 110],
            [0.5, 160],
            [0.75, 200],
            [1.0, 230],
        ],
    }
    return level


def _place_doors(rng, walkable, grid_width, grid_height) -> set[tuple[int, int]]:
    doors: set[tuple[int, int]] = set()
    candidates = []
    for col, row in walkable:
        wall_neighbors = 0
        for d_col, d_row in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbor = (col + d_col, row + d_row)
            inside = 0 <= neighbor[0] < grid_width and 0 <= neighbor[1] < grid_height
            if inside and neighbor not in walkable:
                wall_neighbors += 1
        if wall_neighbors >= 1:
            candidates.append((col, row))
    rng.shuffle(candidates)
    for cell in candidates:
        if len(doors) >= 8:
            break
        if all(
            abs(cell[0] - other[0]) + abs(cell[1] - other[1]) >= 4
            for other in doors
        ):
            doors.add(cell)
    if len(doors) < 4:
        raise RuntimeError("sample generator could not place enough doors")
    return doors


def generate_task(level: dict) -> dict:
    cell_pixels = 8
    return {
        "output_width": level["width"] * cell_pixels,
        "output_height": level["height"] * cell_pixels,
        "cell_pixels": cell_pixels,
        # 384 / 12 == 32 pixel tiles; 288 / 9 == 32, aligned with cells.
        "tile_cols": 12,
        "tile_rows": 9,
        "concurrency": 3,
        # At most four 32x32 tiles (4096 pixels) may be buffered concurrently.
        "in_flight_pixel_limit": 4 * 32 * 32,
        # A round admits roughly eight tiles before yielding.
        "round_budget": 8 * 32 * 32,
        "tolerance": 0,
        "state_dir": "work/state",
        "output_dir": "work/output",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="samples", help="sample output directory (default: samples)"
    )
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    level = generate_level()
    task = generate_task(level)
    (out_dir / "level.json").write_text(
        json.dumps(level, indent=2), encoding="utf-8"
    )
    (out_dir / "render_task.json").write_text(
        json.dumps(task, indent=2), encoding="utf-8"
    )
    room_cells = sum(len(room["cells"]) for room in level["rooms"])
    manifest = {
        "seed": SEED,
        "grid": [level["width"], level["height"]],
        "room_count": len(level["rooms"]),
        "room_cells": room_cells,
        "corridor_cells": len(level["corridors"]),
        "door_count": len(level["doors"]),
        "tile_count": task["tile_cols"] * task["tile_rows"],
        "output": [task["output_width"], task["output_height"]],
    }
    (out_dir / "sample_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
