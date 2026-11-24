from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .errors import SchemaError
from .io_utils import read_json_file


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def require_mapping(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{name} must be a JSON object", {"field": name})
    return value


def require_key(obj: Dict[str, Any], key: str) -> Any:
    if key not in obj:
        raise SchemaError(f"missing required field: {key}", {"field": key})
    return obj[key]


def validate_positive_int(value: Any, name: str) -> int:
    if not is_int(value) or value <= 0:
        raise SchemaError(f"{name} must be a positive integer", {"field": name})
    return int(value)


def validate_nonnegative_int(value: Any, name: str) -> int:
    if not is_int(value) or value < 0:
        raise SchemaError(f"{name} must be a non-negative integer", {"field": name})
    return int(value)


def validate_positive_number(value: Any, name: str) -> float:
    if not is_finite_number(value) or float(value) <= 0.0:
        raise SchemaError(f"{name} must be a finite positive number", {"field": name})
    return float(value)


def validate_vec3(value: Any, name: str) -> List[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 3:
        raise SchemaError(f"{name} must be a three-element numeric array", {"field": name})
    if not all(is_finite_number(item) for item in value):
        raise SchemaError(f"{name} must contain finite numbers", {"field": name})
    return [float(item) for item in value]


def _validate_half_open_edit_region(
    region: Dict[str, Any], heightfield: Dict[str, Any], path: str
) -> Dict[str, int]:
    row_start = require_key(region, "row_start")
    row_end = require_key(region, "row_end_exclusive")
    col_start = require_key(region, "col_start")
    col_end = require_key(region, "col_end_exclusive")
    for name, value in (
        ("row_start", row_start),
        ("row_end_exclusive", row_end),
        ("col_start", col_start),
        ("col_end_exclusive", col_end),
    ):
        if not is_int(value):
            raise SchemaError(
                f"edit region {name} must be an integer grid index",
                {"path": path, "field": f"edit.region.{name}"},
            )

    rows = int(heightfield["global_rows"])
    cols = int(heightfield["global_cols"])
    if not (0 <= row_start < row_end <= rows):
        raise SchemaError(
            "edit row interval must be half-open and inside the global grid",
            {
                "path": path,
                "row_start": row_start,
                "row_end_exclusive": row_end,
                "global_rows": rows,
            },
        )
    if not (0 <= col_start < col_end <= cols):
        raise SchemaError(
            "edit column interval must be half-open and inside the global grid",
            {
                "path": path,
                "col_start": col_start,
                "col_end_exclusive": col_end,
                "global_cols": cols,
            },
        )
    return {
        "row_start": int(row_start),
        "row_end_exclusive": int(row_end),
        "col_start": int(col_start),
        "col_end_exclusive": int(col_end),
    }


def validate_edit_dict(edit: Any, heightfield: Dict[str, Any], path: str = "<edit>") -> Dict[str, Any]:
    edit_obj = require_mapping(edit, "edit")
    if "region" in edit_obj:
        normalized_regions = [
            _validate_half_open_edit_region(
                require_mapping(edit_obj["region"], "edit.region"), heightfield, path
            )
        ]
    elif "regions" in edit_obj:
        regions = edit_obj["regions"]
        if not isinstance(regions, list) or not regions:
            raise SchemaError("edit.regions must be a non-empty array", {"path": path})
        normalized_regions = [
            _validate_half_open_edit_region(
                require_mapping(region, f"edit.regions[{index}]"), heightfield, path
            )
            for index, region in enumerate(regions)
        ]
    else:
        raise SchemaError("edit must declare an integer grid region or regions", {"path": path})
    normalized_region = normalized_regions[0]
    operations = edit_obj.get("operations", [])
    if not isinstance(operations, list):
        raise SchemaError("edit.operations must be an array", {"path": path})
    for index, operation in enumerate(operations):
        operation_obj = require_mapping(operation, f"edit.operations[{index}]")
        if "type" not in operation_obj or not isinstance(operation_obj["type"], str):
            raise SchemaError("edit operation requires a string type", {"path": path})
        if "amplitude" in operation_obj:
            if not is_finite_number(operation_obj["amplitude"]):
                raise SchemaError("edit amplitude must be finite", {"path": path})
        if "center" in operation_obj:
            center = operation_obj["center"]
            if (
                not isinstance(center, Sequence)
                or isinstance(center, (str, bytes))
                or len(center) != 2
                or not all(is_int(item) for item in center)
            ):
                raise SchemaError("edit center must be [col, row] integer indices", {"path": path})
        if "radius" in operation_obj:
            radius = operation_obj["radius"]
            if not is_finite_number(radius) or float(radius) <= 0:
                raise SchemaError("edit radius must be a finite positive number", {"path": path})
    result = dict(edit_obj)
    result["region"] = normalized_region
    result["regions"] = normalized_regions
    return result


def validate_scene(scene: Any, scene_path: str | Path | None = None) -> Dict[str, Any]:
    path = str(scene_path or "<scene>")
    obj = require_mapping(scene, "scene")

    schema_version = obj.get("schema_version", "1.0")
    if not isinstance(schema_version, str):
        raise SchemaError("schema_version must be a string", {"path": path})

    hf = require_mapping(require_key(obj, "heightfield"), "heightfield")
    origin = validate_vec3(require_key(hf, "origin"), "heightfield.origin")
    world_scale = validate_vec3(require_key(hf, "world_scale"), "heightfield.world_scale")
    for axis, value in zip(("x", "y", "z"), world_scale):
        if value <= 0.0:
            raise SchemaError(f"world_scale.{axis} must be positive", {"path": path})

    global_rows = validate_positive_int(require_key(hf, "global_rows"), "heightfield.global_rows")
    global_cols = validate_positive_int(require_key(hf, "global_cols"), "heightfield.global_cols")
    hf["global_rows"] = global_rows
    hf["global_cols"] = global_cols

    bounds = require_mapping(require_key(hf, "world_bounds"), "heightfield.world_bounds")
    bounds_min = validate_vec3(require_key(bounds, "min"), "world_bounds.min")
    bounds_max = validate_vec3(require_key(bounds, "max"), "world_bounds.max")
    if bounds_min[0] >= bounds_max[0] or bounds_min[2] >= bounds_max[2]:
        raise SchemaError("world_bounds x and z maxima must exceed minima", {"path": path})
    expected_max_x = origin[0] + (global_cols - 1) * world_scale[0]
    expected_max_z = origin[2] + (global_rows - 1) * world_scale[2]
    tolerance_x = max(1e-7, abs(expected_max_x) * 1e-9, world_scale[0] * 1e-6)
    tolerance_z = max(1e-7, abs(expected_max_z) * 1e-9, world_scale[2] * 1e-6)
    if abs(bounds_min[0] - origin[0]) > tolerance_x or abs(bounds_max[0] - expected_max_x) > tolerance_x:
        raise SchemaError(
            "global columns and x world scale do not cover the declared world bounds",
            {"path": path, "expected_min_x": origin[0], "expected_max_x": expected_max_x},
        )
    if abs(bounds_min[2] - origin[2]) > tolerance_z or abs(bounds_max[2] - expected_max_z) > tolerance_z:
        raise SchemaError(
            "global rows and z world scale do not cover the declared world bounds",
            {"path": path, "expected_min_z": origin[2], "expected_max_z": expected_max_z},
        )

    tiles_value = require_key(hf, "tiles")
    if not isinstance(tiles_value, list) or not tiles_value:
        raise SchemaError("heightfield.tiles must be a non-empty array", {"path": path})

    ownership = [[-1 for _ in range(global_cols)] for _ in range(global_rows)]
    normalized_tiles: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, tile in enumerate(tiles_value):
        tile_obj = require_mapping(tile, f"heightfield.tiles[{index}]")
        tile_id = require_key(tile_obj, "id")
        file_name = require_key(tile_obj, "file")
        if not isinstance(tile_id, str) or not tile_id:
            raise SchemaError("tile id must be a non-empty string", {"path": path})
        if tile_id in seen_ids:
            raise SchemaError(f"duplicate tile id: {tile_id}", {"path": path})
        seen_ids.add(tile_id)
        if not isinstance(file_name, str) or not file_name:
            raise SchemaError(f"tile {tile_id} file must be a non-empty relative file name", {"path": path})
        row_start = validate_nonnegative_int(tile_obj.get("row_start"), f"tile {tile_id}.row_start")
        col_start = validate_nonnegative_int(tile_obj.get("col_start"), f"tile {tile_id}.col_start")
        rows = validate_positive_int(tile_obj.get("rows"), f"tile {tile_id}.rows")
        cols = validate_positive_int(tile_obj.get("cols"), f"tile {tile_id}.cols")
        row_end = row_start + rows
        col_end = col_start + cols
        if row_end > global_rows or col_end > global_cols:
            raise SchemaError(
                f"tile {tile_id} exceeds the global grid",
                {"path": path, "row_end_exclusive": row_end, "col_end_exclusive": col_end},
            )
        for row in range(row_start, row_end):
            for col in range(col_start, col_end):
                if ownership[row][col] != -1:
                    raise SchemaError(
                        "tile ranges overlap; half-open grid ranges must be unique",
                        {"path": path, "tile_id": tile_id, "row": row, "col": col},
                    )
                ownership[row][col] = index
        normalized_tiles.append(
            {
                "id": tile_id,
                "file": file_name,
                "row_start": row_start,
                "row_end_exclusive": row_end,
                "col_start": col_start,
                "col_end_exclusive": col_end,
                "rows": rows,
                "cols": cols,
            }
        )

    missing = [
        [row, col]
        for row in range(global_rows)
        for col in range(global_cols)
        if ownership[row][col] == -1
    ]
    if missing:
        raise SchemaError(
            "tile half-open ranges do not cover the complete global grid",
            {"path": path, "missing_count": len(missing), "first_missing": missing[:8]},
        )
    hf["tiles"] = normalized_tiles

    camera = require_mapping(require_key(obj, "camera"), "camera")
    position = validate_vec3(require_key(camera, "position"), "camera.position")
    target = validate_vec3(require_key(camera, "target"), "camera.target")
    up = validate_vec3(camera.get("up", [0.0, 1.0, 0.0]), "camera.up")
    if all(abs(position[i] - target[i]) <= 1e-12 for i in range(3)):
        raise SchemaError("camera position and target must not coincide", {"path": path})
    up_norm = math.sqrt(sum(item * item for item in up))
    if up_norm <= 1e-12:
        raise SchemaError("camera.up must be non-zero", {"path": path})
    forward = [target[i] - position[i] for i in range(3)]
    forward_norm = math.sqrt(sum(item * item for item in forward))
    forward_unit = [item / forward_norm for item in forward]
    up_unit = [item / up_norm for item in up]
    parallel = abs(sum(forward_unit[i] * up_unit[i] for i in range(3)))
    if parallel >= 1.0 - 1e-8:
        raise SchemaError(
            "camera up direction must not be parallel to the view direction",
            {"path": path, "abs_dot": parallel},
        )
    fov = validate_positive_number(camera.get("fov_y_degrees", 45.0), "camera.fov_y_degrees")
    if not (1.0 <= fov <= 179.0):
        raise SchemaError("camera fov_y_degrees must be between 1 and 179", {"path": path})
    camera["position"] = position
    camera["target"] = target
    camera["up"] = up
    camera["fov_y_degrees"] = fov
    camera["near"] = float(validate_positive_number(camera.get("near", 0.05), "camera.near"))
    camera["far"] = float(validate_positive_number(camera.get("far", 200.0), "camera.far"))
    if camera["near"] >= camera["far"]:
        raise SchemaError("camera.near must be smaller than camera.far", {"path": path})

    lighting = require_mapping(require_key(obj, "lighting"), "lighting")
    light_direction = validate_vec3(
        lighting.get("direction", [-0.4, 0.85, -0.35]), "lighting.direction"
    )
    light_norm = math.sqrt(sum(item * item for item in light_direction))
    if light_norm <= 1e-12:
        raise SchemaError("lighting.direction must be non-zero", {"path": path})
    lighting["direction"] = light_direction
    ambient = lighting.get("ambient", 0.25)
    intensity = lighting.get("intensity", 0.85)
    if not is_finite_number(ambient) or not (0.0 <= float(ambient) <= 1.0):
        raise SchemaError("lighting.ambient must be in [0,1]", {"path": path})
    if not is_finite_number(intensity) or float(intensity) < 0.0:
        raise SchemaError("lighting.intensity must be non-negative finite", {"path": path})
    lighting["ambient"] = float(ambient)
    lighting["intensity"] = float(intensity)

    resolution = require_mapping(require_key(obj, "resolution"), "resolution")
    width = validate_positive_int(require_key(resolution, "width"), "resolution.width")
    height = validate_positive_int(require_key(resolution, "height"), "resolution.height")
    tile_width = validate_positive_int(resolution.get("tile_width", 64), "resolution.tile_width")
    tile_height = validate_positive_int(resolution.get("tile_height", 64), "resolution.tile_height")
    pixel_density = validate_positive_number(
        require_key(resolution, "pixel_density"), "resolution.pixel_density"
    )
    if tile_width > width or tile_height > height:
        raise SchemaError("tile dimensions must not exceed output dimensions", {"path": path})
    resolution.update(
        {
            "width": width,
            "height": height,
            "tile_width": tile_width,
            "tile_height": tile_height,
            "pixel_density": pixel_density,
        }
    )

    ray_march = obj.get("ray_march", {})
    if not isinstance(ray_march, dict):
        raise SchemaError("ray_march must be an object", {"path": path})
    step_world = validate_positive_number(
        ray_march.get("step_world", max(world_scale[0], world_scale[2]) * 0.75),
        "ray_march.step_world",
    )
    max_steps = validate_positive_int(ray_march.get("max_steps", 256), "ray_march.max_steps")
    refinement_steps = validate_nonnegative_int(
        ray_march.get("refinement_steps", 8), "ray_march.refinement_steps"
    )
    normal_radius_cells = validate_positive_number(
        ray_march.get("normal_radius_cells", 1.0), "ray_march.normal_radius_cells"
    )
    ray_march.update(
        {
            "step_world": step_world,
            "max_steps": max_steps,
            "refinement_steps": refinement_steps,
            "normal_radius_cells": normal_radius_cells,
        }
    )
    obj["ray_march"] = ray_march

    scheduler = obj.get("scheduler", {})
    if not isinstance(scheduler, dict):
        raise SchemaError("scheduler must be an object", {"path": path})
    max_workers = validate_positive_int(scheduler.get("max_workers", 1), "scheduler.max_workers")
    memory_budget = validate_positive_int(
        scheduler.get("memory_budget_bytes", 64 * 1024 * 1024),
        "scheduler.memory_budget_bytes",
    )
    scheduler.update({"max_workers": max_workers, "memory_budget_bytes": memory_budget})
    obj["scheduler"] = scheduler

    output = obj.get("output", {})
    if output is None:
        output = {}
    if not isinstance(output, dict):
        raise SchemaError("output must be an object", {"path": path})
    output.setdefault("grayscale", "terrain_gray.png")
    output.setdefault("depth", "terrain_depth.npy")
    output.setdefault("height", "terrain_height.npy")
    output.setdefault("height_depth", "terrain_height_depth.npz")
    output.setdefault("depth_preview", "terrain_depth.png")
    output.setdefault("normals", "terrain_normals.npy")
    output.setdefault("normal_preview", "terrain_normals.png")
    output.setdefault("report", "bake_report.json")
    output.setdefault("state_dir", "baker_state")
    obj["output"] = output

    edits = obj.get("edits", [])
    if edits is None:
        edits = []
    if not isinstance(edits, list):
        raise SchemaError("edits must be an array", {"path": path})
    obj["edits"] = [validate_edit_dict(item, hf, path) for item in edits]

    obj["schema_version"] = schema_version
    return obj


def load_scene_file(path: str | Path) -> Dict[str, Any]:
    scene_path = Path(path).resolve()
    try:
        raw = read_json_file(scene_path)
    except FileNotFoundError as exc:
        raise SchemaError(f"scene file not found: {scene_path}", {"path": str(scene_path)}) from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(
            "scene JSON is invalid",
            {"path": str(scene_path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise SchemaError(f"scene file cannot be read: {exc}", {"path": str(scene_path)}) from exc
    scene = validate_scene(raw, scene_path)
    scene["_scene_path"] = str(scene_path)
    scene["_scene_dir"] = str(scene_path.parent)
    hf = scene["heightfield"]
    hf_dir = Path(hf.get("directory", "heightfields"))
    if not hf_dir.is_absolute():
        hf_dir = scene_path.parent / hf_dir
    hf["_directory"] = str(hf_dir.resolve())
    for tile in hf["tiles"]:
        tile_path = Path(tile["file"])
        if not tile_path.is_absolute():
            tile_path = hf_dir / tile_path
        tile["_path"] = str(tile_path.resolve())
    return scene


def load_edit_file(path: str | Path, scene: Dict[str, Any]) -> Dict[str, Any]:
    edit_path = Path(path).resolve()
    try:
        edit = read_json_file(edit_path)
    except FileNotFoundError as exc:
        raise SchemaError(f"edit file not found: {edit_path}", {"path": str(edit_path)}) from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(
            "edit JSON is invalid",
            {"path": str(edit_path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise SchemaError(f"edit file cannot be read: {exc}", {"path": str(edit_path)}) from exc
    normalized = validate_edit_dict(edit, scene["heightfield"], str(edit_path))
    normalized["_path"] = str(edit_path)
    return normalized
