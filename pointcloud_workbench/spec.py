"""Task, camera, render, block, and edit-manifest validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import validation_error
from .pointcloud import PointCloud, dataset_digest, load_pointcloud
from .util import (
    digest_json,
    finite_number,
    finite_positive,
    positive_int,
    read_json,
    resolve_local_path,
    safe_component,
)


def _mapping(value: Any, code: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise validation_error(code, f"{field} must be an object", field=field)
    return value


def _vector3(value: Any, code: str, field: str) -> np.ndarray:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise validation_error(code, f"{field} must be a three-element array", field=field)
    return np.asarray(
        [finite_number(item, code, f"{field}[{index}]") for index, item in enumerate(value)],
        dtype=np.float64,
    )


def _nonempty_string(value: Any, code: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise validation_error(code, f"{field} must be a non-empty string", field=field)
    return value


@dataclass(frozen=True)
class CameraSpec:
    camera_id: str
    position: np.ndarray
    target: np.ndarray
    up: np.ndarray
    vertical_fov_deg: float
    aspect_ratio: float

    def normalized(self) -> dict[str, Any]:
        return {
            "id": self.camera_id,
            "position": [float(value) for value in self.position],
            "target": [float(value) for value in self.target],
            "up": [float(value) for value in self.up],
            "vertical_fov_deg": float(self.vertical_fov_deg),
            "aspect_ratio": float(self.aspect_ratio),
        }

    def digest(self) -> str:
        return digest_json(self.normalized())


@dataclass(frozen=True)
class BlockSpec:
    index: int
    row: int
    column: int
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def block_id(self) -> str:
        return f"b{self.index:04d}"

    def rect_dict(self) -> dict[str, int]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


@dataclass
class RenderConfig:
    width: int
    height: int
    supersample: float
    block_width: int
    block_height: int
    max_workers: int
    memory_budget_bytes: int
    round_budget_units: float

    @property
    def supersample_width(self) -> int:
        return max(self.width, int(math.ceil(self.width * self.supersample - 1e-12)))

    @property
    def supersample_height(self) -> int:
        return max(self.height, int(math.ceil(self.height * self.supersample - 1e-12)))

    def supersample_boundary_x(self, output_x: int) -> int:
        return int(round(output_x * self.supersample_width / self.width))

    def supersample_boundary_y(self, output_y: int) -> int:
        return int(round(output_y * self.supersample_height / self.height))

    def block_supersample_rect(self, block: BlockSpec) -> tuple[int, int, int, int]:
        return (
            self.supersample_boundary_x(block.x0),
            self.supersample_boundary_y(block.y0),
            self.supersample_boundary_x(block.x1),
            self.supersample_boundary_y(block.y1),
        )

    def block_sample_buffer_bytes(self, block: BlockSpec) -> int:
        sx0, sy0, sx1, sy1 = self.block_supersample_rect(block)
        return (sx1 - sx0) * (sy1 - sy0) * 16

    def normalized(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "supersample": self.supersample,
            "blocks": {"width": self.block_width, "height": self.block_height},
            "max_workers": self.max_workers,
            "memory_budget_bytes": self.memory_budget_bytes,
            "round_budget_units": float(self.round_budget_units),
        }

    def pixel_parameters(self) -> dict[str, Any]:
        """Parameters that can change pixels; scheduling budgets are excluded."""

        return {
            "width": self.width,
            "height": self.height,
            "supersample": self.supersample,
            "blocks": {"width": self.block_width, "height": self.block_height},
        }

    def digest(self) -> str:
        return digest_json(self.pixel_parameters())

    def blocks(self) -> list[BlockSpec]:
        result: list[BlockSpec] = []
        index = 0
        for y0 in range(0, self.height, self.block_height):
            y1 = min(y0 + self.block_height, self.height)
            for x0 in range(0, self.width, self.block_width):
                x1 = min(x0 + self.block_width, self.width)
                result.append(
                    BlockSpec(
                        index=index,
                        row=len(result) // ((self.width + self.block_width - 1) // self.block_width),
                        column=(index % ((self.width + self.block_width - 1) // self.block_width)),
                        x0=x0,
                        y0=y0,
                        x1=x1,
                        y1=y1,
                    )
                )
                index += 1
        return result

    def validate_coverage(self) -> None:
        total_area = 0
        for block in self.blocks():
            if block.x0 < 0 or block.y0 < 0 or block.x1 > self.width or block.y1 > self.height:
                raise validation_error("E_BLOCKS", "block falls outside output resolution", block=block.block_id)
            total_area += block.width * block.height
        expected_area = self.width * self.height
        if total_area != expected_area:
            raise validation_error(
                "E_BLOCKS",
                "block grid must cover every output pixel exactly once",
                covered_pixels=total_area,
                expected_pixels=expected_area,
            )


@dataclass
class TaskSpec:
    task_id: str
    cache_key: str
    pointcloud: PointCloud
    pointcloud_path: Path
    render: RenderConfig
    cameras: list[CameraSpec]
    task_dir: Path
    task_path: Path
    edit_manifest_path: Path | None
    dataset_digest: str


@dataclass(frozen=True)
class EditRegion:
    region_id: str
    start: int
    count: int
    reason: str

    @property
    def end(self) -> int:
        return self.start + self.count


@dataclass
class EditManifest:
    path: Path
    base_dataset_digest: str
    edited_dataset_digest: str
    regions: list[EditRegion]
    affected_indices: np.ndarray

    def regions_for_indices(self, indices: set[int]) -> list[str]:
        result = []
        for region in self.regions:
            if any(region.start <= index < region.end for index in indices):
                result.append(region.region_id)
        return result


def _parse_camera(value: Any, index: int) -> CameraSpec:
    code = "E_CAMERA"
    source = _mapping(value, "E_SCHEMA", f"cameras[{index}]")
    camera_id = safe_component(
        _nonempty_string(source.get("id"), "E_SCHEMA", f"cameras[{index}].id"),
        "E_SCHEMA",
        f"cameras[{index}].id",
    )
    position = _vector3(source.get("position"), "E_SCHEMA", f"cameras[{index}].position")
    target = _vector3(source.get("target"), "E_SCHEMA", f"cameras[{index}].target")
    up = _vector3(source.get("up"), "E_SCHEMA", f"cameras[{index}].up")
    fov = finite_number(source.get("vertical_fov_deg"), "E_FOV", f"cameras[{index}].vertical_fov_deg")
    if not (0.0 < fov < 180.0):
        raise validation_error(
            "E_FOV",
            "vertical field of view must be strictly between 0 and 180 degrees",
            camera_id=camera_id,
            vertical_fov_deg=fov,
        )
    aspect = finite_positive(source.get("aspect_ratio"), "E_ASPECT", f"cameras[{index}].aspect_ratio")
    forward = target - position
    forward_length = float(np.linalg.norm(forward))
    if not math.isfinite(forward_length) or forward_length <= 0.0:
        raise validation_error(
            code,
            "camera position and target must define a non-zero view direction",
            camera_id=camera_id,
        )
    up_scale = float(np.max(np.abs(up)))
    if not math.isfinite(up_scale) or up_scale <= 0.0:
        raise validation_error(code, "camera up vector must be non-zero", camera_id=camera_id)
    normalized_forward = forward / forward_length
    normalized_up = up / up_scale
    normalized_up /= float(np.linalg.norm(normalized_up))
    if float(np.linalg.norm(np.cross(normalized_forward, normalized_up))) <= 1e-9:
        raise validation_error(
            code,
            "camera up vector is parallel to the view direction",
            camera_id=camera_id,
        )
    position.setflags(write=False)
    target.setflags(write=False)
    up.setflags(write=False)
    return CameraSpec(
        camera_id=camera_id,
        position=position,
        target=target,
        up=up,
        vertical_fov_deg=fov,
        aspect_ratio=aspect,
    )


def _parse_render(value: Any) -> RenderConfig:
    source = _mapping(value, "E_SCHEMA", "render")
    width = positive_int(source.get("width"), "E_BLOCKS", "render.width")
    height = positive_int(source.get("height"), "E_BLOCKS", "render.height")
    supersample = finite_positive(
        source.get("supersample", 1),
        "E_SUPERSAMPLE",
        "render.supersample",
    )
    if not math.isfinite(width * supersample) or not math.isfinite(height * supersample):
        raise validation_error(
            "E_SUPERSAMPLE",
            "supersample multiplier produces an unrepresentable or non-finite sample grid",
            supersample=supersample,
            width=width,
            height=height,
        )
    blocks = _mapping(source.get("blocks"), "E_BLOCKS", "render.blocks")
    block_width = positive_int(blocks.get("width"), "E_BLOCKS", "render.blocks.width")
    block_height = positive_int(blocks.get("height"), "E_BLOCKS", "render.blocks.height")
    max_workers = positive_int(source.get("max_workers", 1), "E_BUDGET", "render.max_workers")
    memory_budget = positive_int(
        source.get("memory_budget_bytes", 64 * 1024 * 1024),
        "E_BUDGET",
        "render.memory_budget_bytes",
    )
    round_budget = finite_positive(
        source.get("round_budget_units", 1000.0),
        "E_BUDGET",
        "render.round_budget_units",
    )
    config = RenderConfig(
        width=width,
        height=height,
        supersample=supersample,
        block_width=block_width,
        block_height=block_height,
        max_workers=max_workers,
        memory_budget_bytes=memory_budget,
        round_budget_units=round_budget,
    )
    config.validate_coverage()
    largest_sample_bytes = max(config.block_sample_buffer_bytes(block) for block in config.blocks())
    if largest_sample_bytes > memory_budget:
        raise validation_error(
            "E_BUDGET",
            "memory budget cannot hold one block's supersampling buffer; supersampling is not reduced",
            largest_block_sample_bytes=largest_sample_bytes,
            memory_budget_bytes=memory_budget,
        )
    return config


def load_task(path: Path) -> TaskSpec:
    path = Path(path).resolve()
    source = _mapping(read_json(path, "E_JSON"), "E_SCHEMA", "task")
    schema_version = source.get("schema_version", 1)
    if schema_version != 1:
        raise validation_error("E_SCHEMA", "unsupported task schema version", schema_version=schema_version)
    task_id = _nonempty_string(source.get("task_id"), "E_SCHEMA", "task_id")
    cache_key = safe_component(
        _nonempty_string(source.get("cache_key"), "E_SCHEMA", "cache_key"),
        "E_SCHEMA",
        "cache_key",
    )
    render = _parse_render(source.get("render"))
    cameras_source = source.get("cameras")
    if not isinstance(cameras_source, list) or not cameras_source:
        raise validation_error("E_SCHEMA", "cameras must be a non-empty array")
    cameras = [_parse_camera(camera, index) for index, camera in enumerate(cameras_source)]
    camera_ids = [camera.camera_id for camera in cameras]
    if len(set(camera_ids)) != len(camera_ids):
        raise validation_error("E_CAMERA", "camera ids must be unique", camera_ids=camera_ids)

    point_source = _mapping(source.get("pointcloud"), "E_SCHEMA", "pointcloud")
    pointcloud_path = resolve_local_path(
        path.parent,
        point_source.get("path"),
        "E_SCHEMA",
        "pointcloud.path",
    )
    world_scale = finite_positive(
        point_source.get("world_scale", 1.0),
        "E_WORLD",
        "pointcloud.world_scale",
    )
    origin_offset = _vector3(
        point_source.get("origin_offset", [0.0, 0.0, 0.0]),
        "E_WORLD",
        "pointcloud.origin_offset",
    )
    cloud = load_pointcloud(pointcloud_path, world_scale, origin_offset)

    manifest_value = source.get("edit_manifest")
    manifest_path = None
    if manifest_value is not None:
        manifest_path = resolve_local_path(
            path.parent,
            manifest_value,
            "E_EDIT",
            "edit_manifest",
        )
    return TaskSpec(
        task_id=task_id,
        cache_key=cache_key,
        pointcloud=cloud,
        pointcloud_path=pointcloud_path,
        render=render,
        cameras=cameras,
        task_dir=path.parent,
        task_path=path,
        edit_manifest_path=manifest_path,
        dataset_digest=dataset_digest(cloud),
    )


def load_edit_manifest(
    path: Path,
    task: TaskSpec,
) -> EditManifest:
    source = _mapping(read_json(path, "E_EDIT"), "E_SCHEMA", "edit_manifest")
    if source.get("schema_version", 1) != 1:
        raise validation_error("E_EDIT", "unsupported edit manifest schema version")
    base_path = resolve_local_path(
        task.task_dir,
        source.get("base_pointcloud"),
        "E_EDIT",
        "base_pointcloud",
    )
    base_cloud = load_pointcloud(
        base_path,
        task.pointcloud.world_scale,
        task.pointcloud.origin_offset,
    )
    base_digest = dataset_digest(base_cloud)
    edited_digest = dataset_digest(task.pointcloud)
    declared_base = _nonempty_string(source.get("base_dataset_digest"), "E_EDIT", "base_dataset_digest")
    declared_edited = _nonempty_string(
        source.get("edited_dataset_digest"),
        "E_EDIT",
        "edited_dataset_digest",
    )
    if declared_base != base_digest:
        raise validation_error(
            "E_EDIT",
            "edit manifest base_dataset_digest does not match base point cloud",
            declared=declared_base,
            actual=base_digest,
        )
    if declared_edited != edited_digest:
        raise validation_error(
            "E_EDIT",
            "edit manifest edited_dataset_digest does not match task point cloud",
            declared=declared_edited,
            actual=edited_digest,
        )
    if base_cloud.point_count != task.pointcloud.point_count:
        raise validation_error(
            "E_EDIT",
            "local replacement requires equal base and edited point counts",
            base_count=base_cloud.point_count,
            edited_count=task.pointcloud.point_count,
        )
    regions_source = source.get("regions")
    if not isinstance(regions_source, list) or not regions_source:
        raise validation_error("E_EDIT", "edit manifest regions must be a non-empty array")
    regions: list[EditRegion] = []
    seen_ids: set[str] = set()
    occupied = np.zeros(task.pointcloud.point_count, dtype=bool)
    affected: set[int] = set()
    for index, item in enumerate(regions_source):
        region_source = _mapping(item, "E_EDIT", f"regions[{index}]")
        region_id = safe_component(
            _nonempty_string(region_source.get("id"), "E_EDIT", f"regions[{index}].id"),
            "E_EDIT",
            f"regions[{index}].id",
        )
        if region_id in seen_ids:
            raise validation_error("E_EDIT", "edit region ids must be unique", region_id=region_id)
        seen_ids.add(region_id)
        start = region_source.get("start")
        count = region_source.get("count")
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise validation_error("E_EDIT", "edit region start must be a non-negative integer", region_id=region_id)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise validation_error("E_EDIT", "edit region count must be a positive integer", region_id=region_id)
        if start + count > task.pointcloud.point_count:
            raise validation_error(
                "E_EDIT",
                "edit region falls outside the point cloud",
                region_id=region_id,
                start=start,
                count=count,
                point_count=task.pointcloud.point_count,
            )
        if occupied[start : start + count].any():
            raise validation_error("E_EDIT", "edit regions must not overlap", region_id=region_id)
        occupied[start : start + count] = True
        old_digest = base_cloud.region_digest(start, count)
        new_digest = task.pointcloud.region_digest(start, count)
        if region_source.get("old_digest") != old_digest:
            raise validation_error(
                "E_EDIT",
                "edit region old_digest does not match base records",
                region_id=region_id,
            )
        if region_source.get("new_digest") != new_digest:
            raise validation_error(
                "E_EDIT",
                "edit region new_digest does not match edited records",
                region_id=region_id,
            )
        reason = _nonempty_string(
            region_source.get("reason", "local rescan replacement"),
            "E_EDIT",
            f"regions[{index}].reason",
        )
        regions.append(EditRegion(region_id=region_id, start=start, count=count, reason=reason))
        affected.update(range(start, start + count))
    return EditManifest(
        path=path,
        base_dataset_digest=base_digest,
        edited_dataset_digest=edited_digest,
        regions=regions,
        affected_indices=np.asarray(sorted(affected), dtype=np.int64),
    )

