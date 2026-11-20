"""Deterministic synthetic scene generation.

All randomness is derived exclusively from the explicit integer seeds passed
in by callers. ``numpy.random.default_rng`` instances are local to each call,
so no global mutable RNG state exists and repeated calls yield byte-identical
arrays.
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

import numpy as np

from .hashing import checksum_numpy
from .manifest import CameraSpec

POINT_COUNT = 240
IMAGE_WIDTH = 32
IMAGE_HEIGHT = 32
CAMERA_RADIUS = 3.2
SCENE_CENTER = np.array([0.5, 0.5, 0.5], dtype=np.float64)


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(np.uint64(seed))


def generate_point_cloud(point_cloud_seed: int, geometry_mode: str,
                         point_count: int = POINT_COUNT) -> np.ndarray:
    """Return an ``(N, 3)`` float64 point cloud inside the unit cube."""
    rng = _rng(point_cloud_seed)
    if geometry_mode == "shell":
        directions = rng.normal(size=(point_count, 3))
        directions /= np.linalg.norm(directions, axis=1, keepdims=True)
        radii = rng.uniform(0.38, 0.46, size=(point_count, 1))
        points = SCENE_CENTER + directions * radii
    elif geometry_mode == "solid":
        points = rng.random((point_count, 3), dtype=np.float64)
    else:
        raise ValueError(f"Unknown geometry_mode: {geometry_mode!r}")
    return np.ascontiguousarray(points, dtype=np.float64)


def generate_point_colors(points: np.ndarray, texture_seed: int) -> np.ndarray:
    """Return deterministic ``(N, 3)`` uint8 per-point colours."""
    rng = _rng(texture_seed)
    base = rng.integers(48, 180, size=3, dtype=np.int64)
    jitter = rng.integers(0, 48, size=(points.shape[0], 3), dtype=np.int64)
    colors = base[None, :] + jitter + (48.0 * points).astype(np.int64)
    return np.ascontiguousarray(np.clip(colors, 0, 255).astype(np.uint8))


def _look_at(eye: np.ndarray, center: np.ndarray, up: np.ndarray) -> np.ndarray:
    z_axis = center - eye
    z_axis /= np.linalg.norm(z_axis)
    x_axis = np.cross(z_axis, up)
    x_axis /= np.linalg.norm(x_axis)
    y_axis = np.cross(x_axis, z_axis)
    rotation = np.stack([x_axis, y_axis, z_axis], axis=0)
    extrinsic = np.eye(4, dtype=np.float64)
    extrinsic[:3, :3] = rotation
    extrinsic[:3, 3] = -rotation @ eye
    return extrinsic


def generate_true_extrinsics(camera_count: int) -> List[np.ndarray]:
    """Deterministic ring of cameras looking at the scene centre."""
    extrinsics: List[np.ndarray] = []
    up = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    for index in range(camera_count):
        angle = 2.0 * math.pi * index / camera_count
        eye = np.array(
            [
                SCENE_CENTER[0] + CAMERA_RADIUS * math.cos(angle),
                SCENE_CENTER[1] + CAMERA_RADIUS * math.sin(angle),
                SCENE_CENTER[2] + 0.35 * math.sin(angle + 0.7),
            ],
            dtype=np.float64,
        )
        extrinsics.append(_look_at(eye, SCENE_CENTER, up))
    return extrinsics


def perturb_extrinsic(extrinsic: np.ndarray, seed: int) -> np.ndarray:
    """Apply a small deterministic rigid perturbation (ICP initial guess)."""
    rng = _rng(seed)
    angle = float(rng.uniform(0.008, 0.02))
    axis = rng.normal(size=3)
    axis /= np.linalg.norm(axis)
    x, y, z = axis
    cross = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=np.float64)
    rotation = np.eye(3) + math.sin(angle) * cross + (1 - math.cos(angle)) * cross @ cross
    delta_t = rng.uniform(-0.008, 0.008, size=3)
    perturbed = np.array(extrinsic, dtype=np.float64, copy=True)
    perturbed[:3, :3] = rotation @ extrinsic[:3, :3]
    perturbed[:3, 3] = extrinsic[:3, 3] + delta_t
    return perturbed


def build_camera_specs(scene_seed: int, camera_count: int) -> Tuple[
        List[CameraSpec], List[np.ndarray]]:
    """Return manifest camera specs (perturbed) and the ground-truth poses."""
    true_extrinsics = generate_true_extrinsics(camera_count)
    specs: List[CameraSpec] = []
    for index, extrinsic in enumerate(true_extrinsics):
        camera_id = f"cam{index:02d}"
        initial = perturb_extrinsic(extrinsic, scene_seed + 101 + index)
        specs.append(
            CameraSpec(
                camera_id=camera_id,
                extrinsic=np.round(initial, 10).tolist(),
                fx=30.0,
                fy=30.0,
                cx=(IMAGE_WIDTH - 1) / 2.0,
                cy=(IMAGE_HEIGHT - 1) / 2.0,
                width=IMAGE_WIDTH,
                height=IMAGE_HEIGHT,
            )
        )
    return specs, true_extrinsics


def render_view(extrinsic: np.ndarray, camera: CameraSpec,
                points: np.ndarray, colors: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Z-buffered point rendering producing depth and colour images."""
    depth = np.zeros((camera.height, camera.width), dtype=np.float64)
    image = np.zeros((camera.height, camera.width, 3), dtype=np.uint8)
    rotation = np.asarray(extrinsic[:3, :3], dtype=np.float64)
    translation = np.asarray(extrinsic[:3, 3], dtype=np.float64)
    camera_points = points @ rotation.T + translation[None, :]
    for point_index in range(points.shape[0]):
        x_cam, y_cam, z_cam = camera_points[point_index]
        if z_cam <= 1e-6:
            continue
        u = int(round(camera.fx * x_cam / z_cam + camera.cx))
        v = int(round(camera.fy * y_cam / z_cam + camera.cy))
        if not (0 <= u < camera.width and 0 <= v < camera.height):
            continue
        if depth[v, u] == 0.0 or z_cam < depth[v, u]:
            depth[v, u] = z_cam
            image[v, u] = colors[point_index]
    return np.ascontiguousarray(depth), np.ascontiguousarray(image)


def render_all_views(cameras: List[CameraSpec], true_extrinsics: List[np.ndarray],
                     points: np.ndarray, colors: np.ndarray
                     ) -> Tuple[Dict[str, np.ndarray], Dict[str, np.ndarray]]:
    depth_maps: Dict[str, np.ndarray] = {}
    color_images: Dict[str, np.ndarray] = {}
    for camera, extrinsic in zip(cameras, true_extrinsics):
        depth, image = render_view(extrinsic, camera, points, colors)
        depth_maps[camera.camera_id] = depth
        color_images[camera.camera_id] = image
    return depth_maps, color_images


def array_summary(points: np.ndarray, colors: np.ndarray,
                  depth_maps: Dict[str, np.ndarray],
                  color_images: Dict[str, np.ndarray]) -> Dict[str, object]:
    """Checksum summary retained as ground truth for independent verification."""
    return {
        "point_cloud_sha256": checksum_numpy(points),
        "colors_sha256": checksum_numpy(colors),
        "depth_sha256": {key: checksum_numpy(value) for key, value in depth_maps.items()},
        "color_image_sha256": {
            key: checksum_numpy(value) for key, value in color_images.items()
        },
        "point_count": int(points.shape[0]),
    }
