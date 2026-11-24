"""Parsing and strict validation of task description and terrain samples.

Validation happens before any output file or state directory is created, so
an invalid input fails with an ``E_xxx`` JSON error and leaves the workspace
untouched.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

from . import errors
from .model import Light, OrthoCamera, TaskSpec, Terrain, TileSpec, normalized


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise errors.fail(errors.E_PARSE, f"file not found: {path}",
                          path=str(path))
    except json.JSONDecodeError as exc:
        raise errors.fail(errors.E_PARSE, f"invalid JSON: {exc.msg}",
                          path=str(path), line=exc.lineno, col=exc.colno)
    if not isinstance(data, dict):
        raise errors.fail(errors.E_PARSE,
                          "top-level JSON document must be an object",
                          path=str(path))
    return data


def _require_positive_int(mapping: Dict[str, Any], key: str,
                          code: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise errors.fail(code, f"{key!r} must be a positive integer",
                          field=key, got=value)
    return int(value)


def _finite_positive(mapping: Dict[str, Any], key: str,
                     code: str = errors.E_SCALE) -> float:
    value = mapping.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(float(value)) or float(value) <= 0:
        raise errors.fail(code,
                          f"{key!r} must be a finite positive number",
                          field=key, got=value)
    return float(value)


def _vec3(mapping: Dict[str, Any], key: str, code: str) -> List[float]:
    value = mapping.get(key)
    if not isinstance(value, list) or len(value) != 3:
        raise errors.fail(code, f"{key!r} must be a 3-component vector",
                          field=key, got=value)
    if not all(isinstance(component, (int, float))
               and not isinstance(component, bool)
               and math.isfinite(float(component))
               for component in value):
        raise errors.fail(code,
                          f"{key!r} components must be finite numbers",
                          field=key, got=value)
    return [float(component) for component in value]


def load_terrain(path: Path) -> Terrain:
    data = _load_json(path)
    rows_nx = _require_positive_int(data, "rows_nx", errors.E_GRID_DIMS)
    rows_ny = _require_positive_int(data, "rows_ny", errors.E_GRID_DIMS)
    scale_x = _finite_positive(data, "scale_x")
    scale_y = _finite_positive(data, "scale_y")
    scale_z = _finite_positive(data, "scale_z")

    raw_heights = data.get("heights")
    if not isinstance(raw_heights, list):
        raise errors.fail(errors.E_HEIGHT_COUNT,
                          "'heights' must be a list",
                          expected=rows_nx * rows_ny, got=None)
    flat: List[int] = []
    for row in raw_heights:
        if not isinstance(row, list):
            raise errors.fail(errors.E_HEIGHT_COUNT,
                              "'heights' must be a list of rows",
                              expected=rows_nx * rows_ny,
                              got=len(flat))
        flat.extend(row)
    expected_count = rows_nx * rows_ny
    if len(flat) != expected_count:
        raise errors.fail(
            errors.E_HEIGHT_COUNT,
            "height count does not match rows_nx * rows_ny",
            expected=expected_count, got=len(flat),
            rows_nx=rows_nx, rows_ny=rows_ny)
    for value in flat:
        if isinstance(value, bool) or not isinstance(value, int):
            raise errors.fail(errors.E_HEIGHT_COUNT,
                              "every height must be a finite integer",
                              got=value)
    heights = np.asarray(flat, dtype=np.int64).reshape(rows_ny, rows_nx)
    return Terrain(rows_nx, rows_ny, scale_x, scale_y, scale_z, heights)


def _build_tiles(spec: Dict[str, Any], width_px: int,
                 height_px: int) -> Tuple[TileSpec, ...]:
    tile_width = _require_positive_int(spec, "tile_width",
                                       errors.E_TILE_COVERAGE)
    tile_height = _require_positive_int(spec, "tile_height",
                                        errors.E_TILE_COVERAGE)
    if width_px % tile_width != 0 or height_px % tile_height != 0:
        raise errors.fail(
            errors.E_TILE_DIVISIBILITY,
            "output resolution must be exactly divisible by tile size",
            width_px=width_px, height_px=height_px,
            tile_width=tile_width, tile_height=tile_height)

    tiles: List[TileSpec] = []
    columns = width_px // tile_width
    row_count = height_px // tile_height
    for row in range(row_count):
        for column in range(columns):
            x = column * tile_width
            y = row * tile_height
            tile_id = f"r{row:04d}_c{column:04d}"
            tiles.append(TileSpec(tile_id, x, y, tile_width, tile_height))
    # Exact coverage guard (also rejects non-exact partial layouts).
    last = tiles[-1]
    if last.x + last.width != width_px or last.y + last.height != height_px:
        raise errors.fail(errors.E_TILE_COVERAGE,
                          "tile grid does not cover the output resolution",
                          width_px=width_px, height_px=height_px)
    return tuple(tiles)


def _build_camera(spec: Dict[str, Any]) -> OrthoCamera:
    position = tuple(_vec3(spec, "position", errors.E_CAMERA_AXIS))
    target = tuple(_vec3(spec, "target", errors.E_CAMERA_AXIS))
    up = tuple(_vec3(spec, "up", errors.E_CAMERA_AXIS))
    view = tuple(target[k] - position[k] for k in range(3))
    if math.isclose(math.sqrt(sum(component ** 2 for component in view)),
                    0.0, abs_tol=1e-12):
        raise errors.fail(errors.E_CAMERA_AXIS,
                          "camera position and target must differ",
                          position=list(position), target=list(target))
    view_unit = normalized(view)
    up_unit = normalized(up)
    cosine = abs(sum(view_unit[k] * up_unit[k] for k in range(3)))
    if cosine >= 1.0 - 1e-9:
        raise errors.fail(
            errors.E_CAMERA_AXIS,
            "camera up direction must not be parallel to the view ray",
            abs_cos=cosine)
    width = _finite_positive(spec, "width", errors.E_CAMERA_AXIS)
    height = _finite_positive(spec, "height", errors.E_CAMERA_AXIS)
    near = _finite_positive(spec, "near", errors.E_CAMERA_AXIS)
    far = _finite_positive(spec, "far", errors.E_CAMERA_AXIS)
    if far <= near:
        raise errors.fail(errors.E_CAMERA_AXIS,
                          "camera 'far' must be greater than 'near'",
                          near=near, far=far)
    return OrthoCamera(position, target, up, width, height, near, far)


def load_task(task_path: Path,
              terrain_override: Path | None = None) -> TaskSpec:
    """Load and validate the full task description.

    ``terrain_override`` lets the CLI point at an alternative terrain file
    (used by tests); otherwise ``terrain_file`` inside the task JSON is
    resolved relative to the task file's directory.
    """
    data = _load_json(task_path)
    base_dir = task_path.resolve().parent
    terrain_file = terrain_override or base_dir / str(
        data.get("terrain_file", ""))
    terrain = load_terrain(Path(terrain_file))

    lights_raw = data.get("lights")
    if not isinstance(lights_raw, list) or len(lights_raw) != 2:
        raise errors.fail(errors.E_LIGHT,
                          "exactly two directional lights are required")
    lights: List[Light] = []
    for index, light_spec in enumerate(lights_raw):
        direction = _vec3(light_spec, "direction", errors.E_LIGHT)
        if math.isclose(math.sqrt(sum(d ** 2 for d in direction)),
                        0.0, abs_tol=1e-12):
            raise errors.fail(errors.E_LIGHT,
                              f"light {index} direction must be non-zero")
        lights.append(Light(normalized(direction)))
    ambient = data.get("ambient")
    if not isinstance(ambient, (int, float)) or isinstance(ambient, bool) \
            or not math.isfinite(float(ambient)) or not 0.0 <= float(ambient) <= 1.0:
        raise errors.fail(errors.E_LIGHT,
                          "'ambient' must be a finite number in [0, 1]",
                          got=ambient)

    camera_spec = data.get("camera")
    if not isinstance(camera_spec, dict):
        raise errors.fail(errors.E_CAMERA_AXIS,
                          "'camera' must be an object")
    camera = _build_camera(camera_spec)

    width_px = _require_positive_int(data, "width_px", errors.E_SCHEMA)
    height_px = _require_positive_int(data, "height_px", errors.E_SCHEMA)
    samples = _require_positive_int(data, "samples", errors.E_SAMPLES)
    tile_spec = data.get("tiles")
    if not isinstance(tile_spec, dict):
        raise errors.fail(errors.E_TILE_COVERAGE,
                          "'tiles' must be an object with tile size")
    tiles = _build_tiles(tile_spec, width_px, height_px)

    max_concurrency = _require_positive_int(
        data, "max_concurrency", errors.E_SCHEMA)
    time_budget = _finite_positive(data, "time_budget_ms", errors.E_BUDGET)
    memory_budget = _require_positive_int(
        data, "memory_budget_bytes", errors.E_BUDGET)

    state_dir = data.get("state_dir")
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise errors.fail(errors.E_SCHEMA,
                          "'state_dir' must be a non-empty string",
                          got=state_dir)

    ray_step = data.get("ray_step_mm")
    if ray_step is None:
        ray_step_mm = min(terrain.scale_x, terrain.scale_y)
    else:
        if not isinstance(ray_step, (int, float)) \
                or isinstance(ray_step, bool) \
                or not math.isfinite(float(ray_step)) or float(ray_step) <= 0:
            raise errors.fail(errors.E_SCHEMA,
                              "'ray_step_mm' must be a positive finite number",
                              got=ray_step)
        ray_step_mm = float(ray_step)

    return TaskSpec(
        terrain=terrain,
        lights=(lights[0], lights[1]),
        ambient=float(ambient),
        camera=camera,
        width_px=width_px,
        height_px=height_px,
        samples=samples,
        tile_width=tiles[0].width,
        tile_height=tiles[0].height,
        max_concurrency=max_concurrency,
        time_budget_ms=time_budget,
        memory_budget_bytes=memory_budget,
        state_dir=state_dir,
        ray_step_mm=ray_step_mm,
        tiles=tiles,
        source=data,
    )

