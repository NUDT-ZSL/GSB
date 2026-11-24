"""Deterministic block-local supersampled point renderer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from .camera import CameraGeometry, ProjectedPoints, _iter_candidate_samples
from .pointcloud import PointCloud
from .spec import BlockSpec, CameraSpec, RenderConfig


@dataclass
class RenderedBlock:
    block: BlockSpec
    gray: np.ndarray
    depth: np.ndarray
    dependency_indices: np.ndarray

    @property
    def pixel_count(self) -> int:
        return int(self.gray.size)


def sample_buffer_bytes(block: BlockSpec, render: RenderConfig) -> int:
    return render.block_sample_buffer_bytes(block)


def logical_cost_units(block: BlockSpec, render: RenderConfig, point_count: int) -> int:
    sx0, sy0, sx1, sy1 = render.block_supersample_rect(block)
    supersamples = (sx1 - sx0) * (sy1 - sy0)
    return max(1, supersamples // 128 + point_count // 128)


def render_block(
    cloud: PointCloud,
    render: RenderConfig,
    block: BlockSpec,
    projection: ProjectedPoints | None = None,
    geometry: CameraGeometry | None = None,
    camera: CameraSpec | None = None,
) -> RenderedBlock:
    if projection is None:
        if geometry is None:
            if camera is None:
                raise ValueError("camera, geometry, or projection is required")
            geometry = CameraGeometry(camera, render)
        projection = geometry.project(cloud)

    sx0, sy0, sx1, sy1 = render.block_supersample_rect(block)
    sample_width = sx1 - sx0
    sample_height = sy1 - sy0
    depth_buffer = np.full((sample_height, sample_width), np.inf, dtype=np.float64)
    winner_buffer = np.full((sample_height, sample_width), -1, dtype=np.int64)
    dependencies: list[int] = []

    for point_index in range(cloud.point_count):
        if not bool(projection.valid[point_index]):
            continue
        center_x = float(projection.center_x[point_index])
        center_y = float(projection.center_y[point_index])
        radius_x = float(projection.radius_x_samples[point_index])
        radius_y = float(projection.radius_y_samples[point_index])
        if (
            center_x + radius_x < sx0
            or center_x - radius_x >= sx1
            or center_y + radius_y < sy0
            or center_y - radius_y >= sy1
        ):
            continue
        _, _, samples = _iter_candidate_samples(
            center_x,
            center_y,
            radius_x,
            radius_y,
            sx0,
            sy0,
            sx1,
            sy1,
        )
        if not samples:
            continue
        dependencies.append(point_index)
        point_depth = float(projection.depth_world[point_index])
        for sample_x, sample_y in samples:
            local_x = sample_x - sx0
            local_y = sample_y - sy0
            old_depth = depth_buffer[local_y, local_x]
            old_index = int(winner_buffer[local_y, local_x])
            if point_depth < old_depth or (
                point_depth == old_depth and (old_index < 0 or point_index < old_index)
            ):
                depth_buffer[local_y, local_x] = point_depth
                winner_buffer[local_y, local_x] = point_index

    if dependencies:
        dependency_array = np.asarray(dependencies, dtype=np.int64)
    else:
        dependency_array = np.empty(0, dtype=np.int64)

    gray_image = np.zeros((block.height, block.width), dtype=np.uint8)
    depth_image = np.full((block.height, block.width), np.inf, dtype=np.float32)
    for output_y in range(block.y0, block.y1):
        group_y0 = render.supersample_boundary_y(output_y) - sy0
        group_y1 = render.supersample_boundary_y(output_y + 1) - sy0
        for output_x in range(block.x0, block.x1):
            group_x0 = render.supersample_boundary_x(output_x) - sx0
            group_x1 = render.supersample_boundary_x(output_x + 1) - sx0
            local_y = output_y - block.y0
            local_x = output_x - block.x0
            winner_group = winner_buffer[group_y0:group_y1, group_x0:group_x1]
            covered = winner_group >= 0
            gray_group = np.zeros(winner_group.shape, dtype=np.float64)
            if covered.any():
                gray_group[covered] = projection.gray_sample[winner_group[covered]]
            mean_gray = float(gray_group.mean(dtype=np.float64))
            gray_image[local_y, local_x] = int(
                np.floor(np.clip(mean_gray, 0.0, 1.0) * 255.0 + 0.5)
            )
            depth_group = depth_buffer[group_y0:group_y1, group_x0:group_x1]
            finite_depth = depth_group[np.isfinite(depth_group)]
            if finite_depth.size:
                depth_image[local_y, local_x] = np.float32(
                    finite_depth.mean(dtype=np.float64)
                )

    return RenderedBlock(
        block=block,
        gray=gray_image,
        depth=depth_image,
        dependency_indices=dependency_array,
    )


def render_camera_reference(
    cloud: PointCloud,
    camera: CameraSpec,
    render: RenderConfig,
) -> tuple[np.ndarray, np.ndarray, list[RenderedBlock]]:
    geometry = CameraGeometry(camera, render)
    projection = geometry.project(cloud)
    gray = np.zeros((render.height, render.width), dtype=np.uint8)
    depth = np.full((render.height, render.width), np.inf, dtype=np.float32)
    blocks: list[RenderedBlock] = []
    for block in render.blocks():
        rendered = render_block(
            cloud=cloud,
            render=render,
            block=block,
            projection=projection,
            geometry=geometry,
            camera=camera,
        )
        gray[block.y0 : block.y1, block.x0 : block.x1] = rendered.gray
        depth[block.y0 : block.y1, block.x0 : block.x1] = rendered.depth
        blocks.append(rendered)
    return gray, depth, blocks


def render_digest(gray: np.ndarray, depth: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(b"PCWB_IMAGE_V1\0")
    digest.update(gray.astype(np.uint8, copy=False).tobytes(order="C"))
    digest.update(b"\0DEPTH\0")
    digest.update(np.asarray(depth, dtype="<f4").tobytes(order="C"))
    return digest.hexdigest()


def compare_images(
    left_gray: np.ndarray,
    left_depth: np.ndarray,
    right_gray: np.ndarray,
    right_depth: np.ndarray,
) -> dict[str, object]:
    gray_equal = bool(np.array_equal(left_gray, right_gray))
    depth_equal = bool(np.array_equal(left_depth, right_depth))
    gray_mismatch = int(np.count_nonzero(left_gray != right_gray))
    if left_depth.shape == right_depth.shape:
        depth_mismatch = int(np.count_nonzero(left_depth != right_depth))
    else:
        depth_mismatch = -1
    return {
        "pixel_identical": gray_equal and depth_equal,
        "gray_identical": gray_equal,
        "depth_identical": depth_equal,
        "gray_mismatch_pixels": gray_mismatch,
        "depth_mismatch_pixels": depth_mismatch,
        "gray_sha256": hashlib.sha256(left_gray.tobytes(order="C")).hexdigest(),
        "depth_sha256": hashlib.sha256(
            np.asarray(left_depth, dtype="<f4").tobytes(order="C")
        ).hexdigest(),
    }

