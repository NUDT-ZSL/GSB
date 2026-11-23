"""Deterministic procedural geometry and seeded observation ingestion."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import PipelineConfig
from .models import CameraPose, SceneSpec
from .rng import rng_for


@dataclass(frozen=True)
class WorldGeometry:
    points: np.ndarray
    faces: np.ndarray
    uv: np.ndarray
    rows: int
    cols: int
    geometry_mode: str

    @property
    def scale(self) -> float:
        centered = self.points - np.mean(self.points, axis=0)
        radius = float(np.sqrt(np.mean(np.sum(centered * centered, axis=1))))
        return max(radius, float(np.finfo(np.float64).tiny))

    @property
    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return np.min(self.points, axis=0), np.max(self.points, axis=0)

    @property
    def diagonal(self) -> float:
        lower, upper = self.bounds
        return max(float(np.linalg.norm(upper - lower)), float(np.finfo(np.float64).tiny))


@dataclass(frozen=True)
class CameraObservation:
    camera: CameraPose
    source_indices: np.ndarray
    source_points_camera: np.ndarray
    camera_image: np.ndarray


def _grid_faces(rows: int, cols: int) -> np.ndarray:
    faces: list[list[int]] = []
    for row in range(rows - 1):
        for col in range(cols - 1):
            a = row * cols + col
            b = row * cols + col + 1
            c = (row + 1) * cols + col
            d = (row + 1) * cols + col + 1
            faces.append([a, b, d])
            faces.append([a, d, c])
    return np.asarray(faces, dtype=np.int64)


def generate_world_geometry(scene: SceneSpec, config: PipelineConfig) -> WorldGeometry:
    """Generate the mode-independent registration cloud.

    Geometry modes are applied after camera-cloud fusion. This keeps the
    registration target stable when only ``geometry_mode`` is revised.
    """

    rows = int(config.grid_rows)
    cols = int(config.grid_cols)
    rng = rng_for("world-geometry", scene.scene_id, scene.point_cloud_seed)

    row_values = np.linspace(0.0, 1.0, rows, dtype=np.float64)
    col_values = np.linspace(0.0, 1.0, cols, dtype=np.float64)
    grid_u, grid_v = np.meshgrid(row_values, col_values, indexing="ij")

    x = -1.25 + 2.5 * grid_v
    y = -1.25 + 2.5 * grid_u
    phase = float(rng.uniform(0.0, 2.0 * np.pi))
    z = (
        0.32 * np.sin(2.1 * x + phase)
        * np.cos(1.7 * y - 0.5 * phase)
        + 0.08 * np.cos(3.1 * (x + y))
    )
    points = np.column_stack((x.ravel(), y.ravel(), z.ravel()))

    jitter = rng.normal(0.0, 0.004, size=points.shape)
    points = np.ascontiguousarray(points + jitter, dtype=np.float64)
    uv = np.column_stack(
        (
            grid_v.ravel(),
            1.0 - grid_u.ravel(),
        )
    ).astype(np.float64, copy=False)
    faces = _grid_faces(rows, cols)
    return WorldGeometry(
        points=points,
        faces=faces,
        uv=np.ascontiguousarray(uv),
        rows=rows,
        cols=cols,
        geometry_mode="registration-base",
    )


def apply_geometry_mode(
    fused_vertices: np.ndarray,
    base_vertices: np.ndarray,
    uv: np.ndarray,
    geometry_mode: str,
    point_cloud_seed: int,
) -> np.ndarray:
    """Apply a deterministic post-fusion geometry representation."""

    fused = np.asarray(fused_vertices, dtype=np.float64)
    base = np.asarray(base_vertices, dtype=np.float64)
    texture_uv = np.asarray(uv, dtype=np.float64)
    if geometry_mode == "terrain":
        return np.ascontiguousarray(fused.copy(), dtype=np.float64)
    if geometry_mode != "ellipsoid":
        raise ValueError(f"unsupported geometry_mode: {geometry_mode}")

    rng = rng_for("geometry-mode", point_cloud_seed, geometry_mode)
    radius_x = 1.25 + float(rng.uniform(-0.04, 0.04))
    radius_y = 1.15 + float(rng.uniform(-0.04, 0.04))
    radius_z = 0.92 + float(rng.uniform(-0.03, 0.03))
    lat = -1.15 + 2.3 * texture_uv[:, 1]
    lon = -np.pi + 2.0 * np.pi * texture_uv[:, 0]
    surface = np.column_stack(
        (
            radius_x * np.cos(lat) * np.cos(lon),
            radius_y * np.cos(lat) * np.sin(lon),
            radius_z * np.sin(lat),
        )
    )
    # Preserve the small camera-fusion correction while changing the mode's
    # base representation.
    correction = fused - base
    return np.ascontiguousarray(surface + correction, dtype=np.float64)


def generate_camera_image(
    scene: SceneSpec,
    camera: CameraPose,
    config: PipelineConfig,
) -> np.ndarray:
    height = int(camera.image_height or config.camera_image_height)
    width = int(camera.image_width or config.camera_image_width)
    yy, xx = np.meshgrid(
        np.arange(height, dtype=np.float64),
        np.arange(width, dtype=np.float64),
        indexing="ij",
    )
    u = xx / max(width - 1, 1)
    v = yy / max(height - 1, 1)
    phase = (
        float(scene.texture_seed % 104729) / 104729.0
        + float(camera.observation_seed % 65537) / 65537.0
    )
    red = (
        0.50
        + 0.22 * np.sin(2.0 * np.pi * (1.3 * u + phase))
        + 0.10 * np.cos(2.0 * np.pi * (0.7 * v - 0.5 * phase))
    )
    green = (
        0.48
        + 0.20 * np.cos(2.0 * np.pi * (1.1 * v + 0.7 * phase))
        + 0.08 * np.sin(2.0 * np.pi * (1.7 * u + 0.3 * phase))
    )
    blue = (
        0.52
        + 0.18 * np.sin(2.0 * np.pi * (0.9 * (u + v) - 0.4 * phase))
        + 0.10 * np.cos(2.0 * np.pi * (1.5 * u - v + phase))
    )
    image = np.stack((red, green, blue), axis=2)
    rng = rng_for(
        "camera-image",
        scene.scene_id,
        camera.camera_id,
        camera.observation_seed,
    )
    image = image + rng.normal(0.0, 0.012, size=image.shape)
    return np.ascontiguousarray(np.clip(image, 0.0, 1.0), dtype=np.float64)


def generate_observations(
    scene: SceneSpec,
    geometry: WorldGeometry,
    config: PipelineConfig,
) -> list[CameraObservation]:
    point_count = int(geometry.points.shape[0])
    sample_count = max(8, int(round(0.72 * point_count)))
    sample_count = min(sample_count, point_count)
    observations: list[CameraObservation] = []

    for camera in sorted(scene.camera_poses, key=lambda item: item.camera_id):
        rng = rng_for(
            "point-observation",
            scene.scene_id,
            scene.point_cloud_seed,
            camera.camera_id,
            camera.observation_seed,
        )
        selected = np.sort(rng.permutation(point_count)[:sample_count]).astype(np.int64)
        world_subset = geometry.points[selected]
        camera_points = (world_subset - camera.translation) @ camera.rotation
        visible = camera_points[:, 2] > 1e-6
        if np.count_nonzero(visible) < 4:
            selected = np.arange(point_count, dtype=np.int64)
            world_subset = geometry.points[selected]
            camera_points = (world_subset - camera.translation) @ camera.rotation
        selected = selected[camera_points[:, 2] > 1e-6]
        camera_points = camera_points[camera_points[:, 2] > 1e-6]
        noise = rng.normal(0.0, float(config.registration_noise), size=camera_points.shape)
        camera_points = np.ascontiguousarray(camera_points + noise, dtype=np.float64)
        image = generate_camera_image(scene, camera, config)
        observations.append(
            CameraObservation(
                camera=camera,
                source_indices=np.ascontiguousarray(selected, dtype=np.int64),
                source_points_camera=camera_points,
                camera_image=image,
            )
        )
    return observations

