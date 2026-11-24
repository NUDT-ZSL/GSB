"""World-space pinhole camera projection and point coverage."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .pointcloud import PointCloud
from .spec import CameraSpec, RenderConfig


MIN_SAMPLE_RADIUS = 0.75


@dataclass
class ProjectedPoints:
    center_x: np.ndarray
    center_y: np.ndarray
    radius_x_samples: np.ndarray
    radius_y_samples: np.ndarray
    depth_world: np.ndarray
    valid: np.ndarray
    gray_sample: np.ndarray
    normal_shade: np.ndarray

    def __post_init__(self) -> None:
        for array in (
            self.center_x,
            self.center_y,
            self.radius_x_samples,
            self.radius_y_samples,
            self.depth_world,
            self.valid,
            self.gray_sample,
            self.normal_shade,
        ):
            array.setflags(write=False)


class CameraGeometry:
    def __init__(self, camera: CameraSpec, render: RenderConfig) -> None:
        self.camera = camera
        self.supersample_width = render.supersample_width
        self.supersample_height = render.supersample_height
        forward = camera.target - camera.position
        forward_length = float(np.linalg.norm(forward))
        self.forward = forward / forward_length
        up_scale = float(np.max(np.abs(camera.up)))
        up = camera.up / up_scale
        up /= float(np.linalg.norm(up))
        right = np.cross(self.forward, up)
        right_length = float(np.linalg.norm(right))
        if right_length <= 1e-12:
            # Task validation normally catches this; kept as a hard guard.
            raise ValueError("camera up is parallel to the view direction")
        self.right = right / right_length
        self.true_up = np.cross(self.right, self.forward)
        self.true_up /= float(np.linalg.norm(self.true_up))
        self.tan_half_fov_y = math.tan(math.radians(camera.vertical_fov_deg) * 0.5)
        self.focal_y = (self.supersample_height * 0.5) / self.tan_half_fov_y
        self.focal_x = self.supersample_width / (
            2.0 * self.tan_half_fov_y * camera.aspect_ratio
        )
        self.camera_distance = forward_length

    def project(self, cloud: PointCloud) -> ProjectedPoints:
        relative = cloud.points_world - self.camera.position[None, :]
        camera_x = relative @ self.right
        camera_y = relative @ self.true_up
        depth = relative @ self.forward
        valid = np.isfinite(depth) & (depth > 0.0)
        safe_depth = np.where(valid, depth, 1.0)
        center_x = self.supersample_width * 0.5 + (camera_x / safe_depth) * self.focal_x
        center_y = self.supersample_height * 0.5 - (camera_y / safe_depth) * self.focal_y
        projected_radius_x = self.focal_x * cloud.radius_world / safe_depth
        projected_radius_y = self.focal_y * cloud.radius_world / safe_depth
        diagonal = math.hypot(self.supersample_width, self.supersample_height)
        projected_radius_x = np.where(
            np.isfinite(projected_radius_x),
            projected_radius_x,
            diagonal,
        )
        projected_radius_y = np.where(
            np.isfinite(projected_radius_y),
            projected_radius_y,
            diagonal,
        )
        radius_x_samples = np.maximum(np.clip(projected_radius_x, 0.0, diagonal), MIN_SAMPLE_RADIUS)
        radius_y_samples = np.maximum(np.clip(projected_radius_y, 0.0, diagonal), MIN_SAMPLE_RADIUS)
        valid &= (
            (center_x + radius_x_samples >= 0.0)
            & (center_x - radius_x_samples <= self.supersample_width)
            & (center_y + radius_y_samples >= 0.0)
            & (center_y - radius_y_samples <= self.supersample_height)
        )
        to_camera = self.camera.position[None, :] - cloud.points_world
        to_camera_length = np.linalg.norm(to_camera, axis=1)
        safe_to_camera = np.where(to_camera_length > 0.0, to_camera_length, 1.0)
        view_direction = to_camera / safe_to_camera[:, None]
        lambert = np.clip(np.einsum("ij,ij->i", cloud.normals, view_direction), 0.0, 1.0)
        normal_shade = 0.35 + 0.65 * lambert
        attenuation = 1.0 / (
            1.0 + 0.25 * np.maximum(depth, 0.0) / self.camera_distance
        )
        gray_sample = np.clip(cloud.gray * normal_shade * attenuation, 0.0, 1.0)
        return ProjectedPoints(
            center_x=np.asarray(center_x, dtype=np.float64),
            center_y=np.asarray(center_y, dtype=np.float64),
            radius_x_samples=np.asarray(radius_x_samples, dtype=np.float64),
            radius_y_samples=np.asarray(radius_y_samples, dtype=np.float64),
            depth_world=np.asarray(depth, dtype=np.float64),
            valid=np.asarray(valid, dtype=bool),
            gray_sample=np.asarray(gray_sample, dtype=np.float64),
            normal_shade=np.asarray(normal_shade, dtype=np.float64),
        )


def block_index_map(render: RenderConfig) -> np.ndarray:
    mapping = np.full((render.supersample_height, render.supersample_width), -1, dtype=np.int64)
    for block in render.blocks():
        sx0, sy0, sx1, sy1 = render.block_supersample_rect(block)
        mapping[sy0:sy1, sx0:sx1] = block.index
    return mapping


def _iter_candidate_samples(
    center_x: float,
    center_y: float,
    radius_x: float,
    radius_y: float,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> tuple[int, int, list[tuple[int, int]]]:
    min_x = max(x0, int(math.floor(center_x - radius_x)))
    max_x = min(x1 - 1, int(math.ceil(center_x + radius_x)) - 1)
    min_y = max(y0, int(math.floor(center_y - radius_y)))
    max_y = min(y1 - 1, int(math.ceil(center_y + radius_y)) - 1)
    if min_x > max_x or min_y > max_y:
        return 0, 0, []
    samples: list[tuple[int, int]] = []
    for sample_y in range(min_y, max_y + 1):
        offset_y = (sample_y + 0.5 - center_y) / radius_y
        for sample_x in range(min_x, max_x + 1):
            offset_x = (sample_x + 0.5 - center_x) / radius_x
            if offset_x * offset_x + offset_y * offset_y <= 1.0 + 1e-12:
                samples.append((sample_x, sample_y))
    return min_x, min_y, samples


def point_block_hits(
    projection: ProjectedPoints,
    indices: np.ndarray,
    render: RenderConfig,
) -> dict[int, set[int]]:
    """Return blocks whose supersample pixels a set of points can cover."""

    mapping = block_index_map(render)
    height, width = mapping.shape
    hits: dict[int, set[int]] = {}
    for raw_index in indices:
        index = int(raw_index)
        if not bool(projection.valid[index]):
            continue
        _, _, samples = _iter_candidate_samples(
            float(projection.center_x[index]),
            float(projection.center_y[index]),
            float(projection.radius_x_samples[index]),
            float(projection.radius_y_samples[index]),
            0,
            0,
            width,
            height,
        )
        for sample_x, sample_y in samples:
            block_index = int(mapping[sample_y, sample_x])
            if block_index >= 0:
                hits.setdefault(block_index, set()).add(index)
    return hits

