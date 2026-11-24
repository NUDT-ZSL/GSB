"""JSON parsing and strict schema validation.

Every validation failure raises :class:`DungeonError` with ``E_SCHEMA``.
Validation runs before any output or state directory is touched, so an
invalid document can never produce a partial image.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .errors import DungeonError
from .models import Level, RenderTask


def _schema(message: str, **details) -> DungeonError:
    return DungeonError("E_SCHEMA", message, details)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(obj: dict, key: str, *, minimum: int = 0) -> int:
    if key not in obj:
        raise _schema(f"missing integer field '{key}'", field=key)
    value = obj[key]
    if not _is_int(value):
        raise _schema(f"field '{key}' must be an integer", field=key)
    if value < minimum:
        raise _schema(
            f"field '{key}' must be >= {minimum}", field=key, value=value
        )
    return value


def _parse_cell(value, label: str) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not _is_int(value[0])
        or not _is_int(value[1])
    ):
        raise _schema(f"{label} must be an [col, row] integer pair", value=value)
    return int(value[0]), int(value[1])


def load_json(path: str | Path) -> dict:
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise _schema(f"JSON file not found: {target}", path=str(target)) from exc
    except json.JSONDecodeError as exc:
        raise _schema(
            f"invalid JSON in {target}: {exc.msg}",
            path=str(target),
            line=exc.lineno,
            column=exc.colno,
        ) from exc
    if not isinstance(data, dict):
        raise _schema(f"top-level JSON document must be an object: {target}")
    return data


def parse_level(data: dict) -> Level:
    width = _require_int(data, "width", minimum=1)
    height = _require_int(data, "height", minimum=1)

    rooms_raw = data.get("rooms")
    if not isinstance(rooms_raw, list) or not rooms_raw:
        raise _schema("'rooms' must be a non-empty list")

    rooms: dict[tuple[int, int], int] = {}
    room_bounds: dict[int, tuple[int, int, int, int]] = {}
    for entry in rooms_raw:
        if not isinstance(entry, dict) or "id" not in entry or "cells" not in entry:
            raise _schema("each room needs integer 'id' and 'cells' list")
        room_id = entry["id"]
        if not _is_int(room_id):
            raise _schema("room id must be an integer")
        if room_id in room_bounds:
            raise _schema("duplicate room id", room_id=room_id)
        cells_raw = entry["cells"]
        if not isinstance(cells_raw, list) or not cells_raw:
            raise _schema("room cells must be a non-empty list", room_id=room_id)
        cols: list[int] = []
        rows: list[int] = []
        for raw in cells_raw:
            cell = _parse_cell(raw, f"room {room_id} cell")
            col, row = cell
            if not (0 <= col < width and 0 <= row < height):
                raise _schema(
                    "room cell outside grid", room_id=room_id, cell=list(cell)
                )
            if cell in rooms:
                raise _schema(
                    "duplicate cell inside room list", cell=list(cell)
                )
            rooms[cell] = room_id
            cols.append(col)
            rows.append(row)
        room_bounds[room_id] = (min(cols), min(rows), max(cols), max(rows))

    corridors_raw = data.get("corridors")
    if not isinstance(corridors_raw, list) or not corridors_raw:
        raise _schema("'corridors' must be a non-empty list")
    corridors: set[tuple[int, int]] = set()
    for raw in corridors_raw:
        cell = _parse_cell(raw, "corridor cell")
        col, row = cell
        if not (0 <= col < width and 0 <= row < height):
            raise _schema("corridor cell outside grid", cell=list(cell))
        if cell in rooms:
            raise _schema("cell belongs to both room and corridor", cell=list(cell))
        if cell in corridors:
            raise _schema("duplicate corridor cell", cell=list(cell))
        corridors.add(cell)

    walkable = frozenset(rooms) | frozenset(corridors)
    all_cells = {(c, r) for c in range(width) for r in range(height)}
    walls = all_cells - walkable

    doors_raw = data.get("doors")
    if not isinstance(doors_raw, list):
        raise _schema("'doors' must be a list")
    doors: list[tuple[int, int]] = []
    for raw in doors_raw:
        cell = _parse_cell(raw, "door")
        col, row = cell
        if not (0 <= col < width and 0 <= row < height):
            raise _schema("door outside grid", cell=list(cell))
        if cell not in walkable:
            raise _schema("door must be placed on a walkable cell", cell=list(cell))
        neighbors = ((col + 1, row), (col - 1, row), (col, row + 1), (col, row - 1))
        if not any(neighbor in walls for neighbor in neighbors):
            raise _schema(
                "door must be adjacent to at least one wall cell", cell=list(cell)
            )
        if cell in doors:
            raise _schema("duplicate door cell", cell=list(cell))
        doors.append(cell)

    wall_thickness = _require_int(data, "wall_thickness", minimum=1)
    side_lengths = [
        max_col - min_col + 1
        for min_col, _min_row, max_col, _max_row in room_bounds.values()
    ]
    side_lengths += [
        max_row - min_row + 1
        for _min_col, min_row, _max_col, max_row in room_bounds.values()
    ]
    min_side = min(side_lengths)
    if wall_thickness > min_side:
        raise _schema(
            "wall_thickness must not exceed the smallest room side length",
            wall_thickness=wall_thickness,
            min_room_side=min_side,
        )

    light_radius = data.get("light_radius")
    if isinstance(light_radius, bool) or not isinstance(light_radius, (int, float)):
        raise _schema("light_radius must be a finite positive number")
    light_radius = float(light_radius)
    if not math.isfinite(light_radius) or light_radius <= 0:
        raise _schema("light_radius must be a finite positive number")

    palette = _parse_palette(data.get("palette"))

    return Level(
        width=width,
        height=height,
        rooms=rooms,
        corridors=frozenset(corridors),
        doors=tuple(doors),
        wall_thickness=wall_thickness,
        light_radius=light_radius,
        palette=tuple(palette),
    )


def _parse_palette(raw) -> list[tuple[float, int]]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise _schema("palette must contain at least two [t, gray] control points")
    points: list[tuple[float, int]] = []
    previous_t = -math.inf
    previous_gray = -1
    for entry in raw:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 2
            or isinstance(entry[0], bool)
            or not isinstance(entry[0], (int, float))
            or not _is_int(entry[1])
        ):
            raise _schema("palette entries must be [number, integer] pairs")
        t_value = float(entry[0])
        gray = int(entry[1])
        if not math.isfinite(t_value) or not 0.0 <= t_value <= 1.0:
            raise _schema("palette control position must be within [0, 1]")
        if not 0 <= gray <= 255:
            raise _schema("palette gray value must be within [0, 255]")
        if t_value <= previous_t:
            raise _schema("palette control positions must be strictly increasing")
        if gray < previous_gray:
            raise _schema("palette gray values must be non-decreasing")
        points.append((t_value, gray))
        previous_t = t_value
        previous_gray = gray
    if points[0][0] != 0.0 or points[-1][0] != 1.0:
        raise _schema("palette must span control positions 0.0 through 1.0")
    return points


def parse_task(data: dict, level: Level) -> RenderTask:
    cell_pixels = _require_int(data, "cell_pixels", minimum=1)
    tile_cols = _require_int(data, "tile_cols", minimum=1)
    tile_rows = _require_int(data, "tile_rows", minimum=1)
    expected_width = level.width * cell_pixels
    expected_height = level.height * cell_pixels
    output_width = _require_int(data, "output_width", minimum=1)
    output_height = _require_int(data, "output_height", minimum=1)
    if output_width != expected_width or output_height != expected_height:
        raise _schema(
            "output resolution must equal grid size * cell_pixels",
            expected=[expected_width, expected_height],
            actual=[output_width, output_height],
        )
    if output_width % tile_cols != 0 or output_height % tile_rows != 0:
        raise _schema(
            "tile grid must divide output resolution into complete tiles",
            output=[output_width, output_height],
            tile_grid=[tile_cols, tile_rows],
        )

    concurrency = _require_int(data, "concurrency", minimum=1)
    in_flight_limit = _require_int(data, "in_flight_pixel_limit", minimum=1)
    round_budget = _require_int(data, "round_budget", minimum=1)
    tolerance = _require_int(data, "tolerance", minimum=0)

    tile_w = output_width // tile_cols
    tile_h = output_height // tile_rows
    largest_tile = tile_w * tile_h
    if in_flight_limit < largest_tile:
        raise _schema(
            "in_flight_pixel_limit must hold at least one complete tile",
            limit=in_flight_limit,
            largest_tile_pixels=largest_tile,
        )

    for key in ("state_dir", "output_dir"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise _schema(f"'{key}' must be a non-empty string path")

    return RenderTask(
        output_width=output_width,
        output_height=output_height,
        cell_pixels=cell_pixels,
        tile_cols=tile_cols,
        tile_rows=tile_rows,
        concurrency=concurrency,
        in_flight_pixel_limit=in_flight_limit,
        round_budget=round_budget,
        tolerance=tolerance,
        state_dir=data["state_dir"],
        output_dir=data["output_dir"],
    )


def load_level_and_task(
    level_path: str | Path, task_path: str | Path
) -> tuple[Level, RenderTask]:
    level = parse_level(load_json(level_path))
    task = parse_task(load_json(task_path), level)
    return level, task
