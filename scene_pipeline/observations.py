"""On-disk layout and I/O for deterministic multi-modal observations."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List

import numpy as np

from .hashing import checksum_numpy
from .manifest import CameraSpec, SceneManifest
from .synth import (
    array_summary,
    build_camera_specs,
    generate_point_cloud,
    generate_point_colors,
    POINT_COUNT,
    render_all_views,
)

POINT_CLOUD_FILE = "point_cloud.npy"
POINT_COLORS_FILE = "point_colors.npy"
GROUND_TRUTH_FILE = "ground_truth.json"


def observation_dir(base_dir: str, scene_id: str) -> str:
    return os.path.join(base_dir, scene_id)


def depth_path(base_dir: str, scene_id: str, camera_id: str) -> str:
    return os.path.join(observation_dir(base_dir, scene_id), f"depth_{camera_id}.npy")


def color_path(base_dir: str, scene_id: str, camera_id: str) -> str:
    return os.path.join(observation_dir(base_dir, scene_id), f"color_{camera_id}.npy")


def save_npy(path: str, array: np.ndarray) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        np.save(handle, np.ascontiguousarray(array), allow_pickle=False)


def load_npy(path: str) -> np.ndarray:
    with open(path, "rb") as handle:
        return np.load(handle, allow_pickle=False)


def generate_scene_bundle(manifest: SceneManifest, base_dir: str,
                          point_count: int | None = None) -> Dict[str, object]:
    """Generate and persist all observations for one scene; return ground truth."""
    scene_dir = observation_dir(base_dir, manifest.scene_id)
    os.makedirs(scene_dir, exist_ok=True)

    cameras, true_extrinsics = build_camera_specs(
        manifest.point_cloud_seed, len(manifest.camera_poses)
    )
    # The manifest passed in already carries the same perturbed camera specs;
    # regenerate only when an externally supplied manifest lacks them.
    if not manifest.camera_poses:
        manifest = SceneManifest(**{**manifest.__dict__, "camera_poses": cameras})

    points = generate_point_cloud(
        manifest.point_cloud_seed, manifest.geometry_mode,
        point_count=point_count or POINT_COUNT,
    )
    colors = generate_point_colors(points, manifest.texture_seed)
    depth_maps, color_images = render_all_views(
        manifest.camera_poses, true_extrinsics, points, colors
    )

    save_npy(os.path.join(scene_dir, POINT_CLOUD_FILE), points)
    save_npy(os.path.join(scene_dir, POINT_COLORS_FILE), colors)
    for camera in manifest.camera_poses:
        save_npy(depth_path(base_dir, manifest.scene_id, camera.camera_id),
                 depth_maps[camera.camera_id])
        save_npy(color_path(base_dir, manifest.scene_id, camera.camera_id),
                 color_images[camera.camera_id])

    ground_truth = {
        "scene_id": manifest.scene_id,
        "scene_revision": manifest.scene_revision,
        "true_extrinsics": [np.round(mat, 12).tolist() for mat in true_extrinsics],
        "camera_ids": [camera.camera_id for camera in manifest.camera_poses],
        "summary": array_summary(points, colors, depth_maps, color_images),
    }
    with open(os.path.join(scene_dir, GROUND_TRUTH_FILE), "w",
              encoding="utf-8") as handle:
        json.dump(ground_truth, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return ground_truth


@dataclass
class ObservationBundle:
    manifest: SceneManifest
    points: np.ndarray
    colors: np.ndarray
    depth_maps: Dict[str, np.ndarray]
    color_images: Dict[str, np.ndarray]
    ground_truth: Dict[str, object]

    @property
    def cameras(self) -> List[CameraSpec]:
        return self.manifest.camera_poses


def load_ground_truth(base_dir: str, scene_id: str) -> Dict[str, object]:
    path = os.path.join(observation_dir(base_dir, scene_id), GROUND_TRUTH_FILE)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_observation_bundle(manifest: SceneManifest, base_dir: str,
                            verify: bool = True) -> ObservationBundle:
    """Load observations and re-derive/checksum everything from explicit seeds."""
    from .errors import IngestError

    scene_dir = observation_dir(base_dir, manifest.scene_id)
    scene_id = manifest.scene_id
    try:
        points = load_npy(os.path.join(scene_dir, POINT_CLOUD_FILE))
        colors = load_npy(os.path.join(scene_dir, POINT_COLORS_FILE))
    except (OSError, ValueError) as exc:
        raise IngestError(
            f"missing or unreadable point cloud: {exc}",
            scene_id=scene_id, stage="ingest",
        ) from exc

    ground_truth = load_ground_truth(base_dir, scene_id)
    expected_count = int(ground_truth["summary"]["point_count"])
    expected_points = generate_point_cloud(
        manifest.point_cloud_seed, manifest.geometry_mode,
        point_count=expected_count,
    )
    if checksum_numpy(points) != checksum_numpy(expected_points):
        raise IngestError(
            "stored point cloud does not match point_cloud_seed",
            scene_id=scene_id, stage="ingest",
            details={"stored_sha256": checksum_numpy(points)},
        )

    depth_maps: Dict[str, np.ndarray] = {}
    color_images: Dict[str, np.ndarray] = {}
    for camera in manifest.camera_poses:
        try:
            depth = load_npy(depth_path(base_dir, scene_id, camera.camera_id))
            image = load_npy(color_path(base_dir, scene_id, camera.camera_id))
        except (OSError, ValueError) as exc:
            raise IngestError(
                f"missing observation for camera {camera.camera_id}: {exc}",
                scene_id=scene_id, stage="ingest",
            ) from exc
        if depth.shape != (camera.height, camera.width):
            raise IngestError(
                f"depth shape mismatch for camera {camera.camera_id}",
                scene_id=scene_id, stage="ingest",
                details={"shape": list(depth.shape)},
            )
        if image.shape != (camera.height, camera.width, 3):
            raise IngestError(
                f"color image shape mismatch for camera {camera.camera_id}",
                scene_id=scene_id, stage="ingest",
                details={"shape": list(image.shape)},
            )
        depth_maps[camera.camera_id] = depth
        color_images[camera.camera_id] = image

    if verify:
        summary = ground_truth["summary"]
        if checksum_numpy(points) != summary["point_cloud_sha256"]:
            raise IngestError("point cloud checksum mismatch vs ground truth",
                               scene_id=scene_id, stage="ingest")
        for camera_id, depth in depth_maps.items():
            if checksum_numpy(depth) != summary["depth_sha256"][camera_id]:
                raise IngestError(
                    f"depth checksum mismatch for {camera_id}",
                    scene_id=scene_id, stage="ingest")
        for camera_id, image in color_images.items():
            if checksum_numpy(image) != summary["color_image_sha256"][camera_id]:
                raise IngestError(
                    f"color image checksum mismatch for {camera_id}",
                    scene_id=scene_id, stage="ingest")

    return ObservationBundle(
        manifest=manifest,
        points=points,
        colors=colors,
        depth_maps=depth_maps,
        color_images=color_images,
        ground_truth=ground_truth,
    )
