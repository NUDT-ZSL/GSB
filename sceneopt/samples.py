"""Built-in deterministic sample manifests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from .serialization import canonical_text


@dataclass(frozen=True)
class SampleCameraSpec:
    position: tuple[float, float, float]
    target: tuple[float, float, float]


def _look_at(position: tuple[float, float, float], target: tuple[float, float, float]) -> np.ndarray:
    position_array = np.asarray(position, dtype=np.float64)
    target_array = np.asarray(target, dtype=np.float64)
    forward = target_array - position_array
    forward = forward / np.linalg.norm(forward)
    up = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
    right = np.cross(forward, up)
    right = right / np.linalg.norm(right)
    down = np.cross(forward, right)
    down = down / np.linalg.norm(down)
    return np.column_stack((right, down, forward))


def _camera(
    scene_id: str,
    index: int,
    position: tuple[float, float, float],
    observation_seed: int,
) -> dict[str, Any]:
    rotation = _look_at(position, (0.0, 0.0, 0.05))
    width = 24
    height = 18
    focal = 24.0
    intrinsics = np.asarray(
        (
            (focal, 0.0, (width - 1) / 2.0),
            (0.0, focal, (height - 1) / 2.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    return {
        "camera_id": f"{scene_id}-cam-{index}",
        "rotation": rotation.tolist(),
        "translation": list(position),
        "intrinsics": intrinsics.tolist(),
        "observation_seed": int(observation_seed),
        "image_height": height,
        "image_width": width,
    }


def _scene(
    scene_id: str,
    *,
    revision: int,
    point_cloud_seed: int,
    texture_seed: int,
    geometry_mode: str,
    target_face_budget: int,
    priority: int,
    observation_seeds: tuple[int, int, int],
) -> dict[str, Any]:
    positions = (
        (2.8, 0.1, 1.65),
        (-2.25, 1.75, 1.45),
        (1.55, -2.4, 1.30),
    )
    cameras = [
        _camera(scene_id, index, position, observation_seeds[index])
        for index, position in enumerate(positions)
    ]
    return {
        "scene_id": scene_id,
        "scene_revision": int(revision),
        "camera_poses": cameras,
        "point_cloud_seed": int(point_cloud_seed),
        "texture_seed": int(texture_seed),
        "geometry_mode": geometry_mode,
        "target_face_budget": int(target_face_budget),
        "priority": int(priority),
    }


def baseline_manifest() -> list[dict[str, Any]]:
    return [
        _scene(
            "scene-alpha",
            revision=1,
            point_cloud_seed=1103,
            texture_seed=2203,
            geometry_mode="terrain",
            target_face_budget=72,
            priority=30,
            observation_seeds=(1001, 1002, 1003),
        ),
        _scene(
            "scene-beta",
            revision=1,
            point_cloud_seed=1207,
            texture_seed=2309,
            geometry_mode="ellipsoid",
            target_face_budget=64,
            priority=20,
            observation_seeds=(2001, 2002, 2004),
        ),
        _scene(
            "scene-delta",
            revision=1,
            point_cloud_seed=1301,
            texture_seed=2407,
            geometry_mode="terrain",
            target_face_budget=64,
            priority=10,
            observation_seeds=(3001, 3002, 3003),
        ),
        _scene(
            "scene-gamma",
            revision=1,
            point_cloud_seed=1409,
            texture_seed=2503,
            geometry_mode="terrain",
            target_face_budget=80,
            priority=40,
            observation_seeds=(4001, 4002, 4003),
        ),
    ]


def revision_manifest() -> list[dict[str, Any]]:
    baseline = {scene["scene_id"]: scene for scene in baseline_manifest()}

    alpha = _scene(
        "scene-alpha",
        revision=2,
        point_cloud_seed=1103,
        texture_seed=2203,
        geometry_mode="terrain",
        target_face_budget=72,
        priority=30,
        observation_seeds=(1001, 9102, 1003),
    )
    beta = _scene(
        "scene-beta",
        revision=2,
        point_cloud_seed=1207,
        texture_seed=2309,
        geometry_mode="ellipsoid",
        target_face_budget=48,
        priority=20,
        observation_seeds=(2001, 2002, 2004),
    )
    delta = baseline["scene-delta"]
    gamma = _scene(
        "scene-gamma",
        revision=2,
        point_cloud_seed=1409,
        texture_seed=2503,
        geometry_mode="ellipsoid",
        target_face_budget=80,
        priority=40,
        observation_seeds=(4001, 4002, 4003),
    )
    return sorted((alpha, beta, delta, gamma), key=lambda scene: scene["scene_id"])


def _write_jsonl(path: str, scenes: list[dict[str, Any]]) -> None:
    text = "\n".join(canonical_text(scene) for scene in scenes) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def ensure_samples(root: str = "samples", *, force: bool = False) -> dict[str, str]:
    os.makedirs(root, exist_ok=True)
    baseline_path = os.path.join(root, "scenes.jsonl")
    revision_path = os.path.join(root, "revision.jsonl")
    if force or not os.path.isfile(baseline_path):
        _write_jsonl(baseline_path, baseline_manifest())
    if force or not os.path.isfile(revision_path):
        _write_jsonl(revision_path, revision_manifest())
    readme_path = os.path.join(root, "README.md")
    if force or not os.path.isfile(readme_path):
        with open(readme_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "# Deterministic samples\n\n"
                "`scenes.jsonl` is the baseline manifest. `revision.jsonl` changes "
                "one camera observation in `scene-alpha`, the face budget in "
                "`scene-beta`, and the geometry mode in `scene-gamma`; "
                "`scene-delta` is unchanged and must be fully reused.\n"
            )
    return {
        "baseline": baseline_path,
        "revision": revision_path,
        "readme": readme_path,
    }

