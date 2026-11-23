"""Scene manifest data models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .serialization import content_checksum

VALID_GEOMETRY_MODES = ("terrain", "ellipsoid")


def _matrix3(value: Any, field_name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (3, 3):
        raise ValueError(f"{field_name} must have shape (3, 3)")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{field_name} contains a non-finite number")
    return array.copy()


@dataclass(frozen=True)
class CameraPose:
    camera_id: str
    rotation: np.ndarray
    translation: np.ndarray
    intrinsics: np.ndarray
    observation_seed: int
    image_height: int = 18
    image_width: int = 24

    def __post_init__(self) -> None:
        rotation = _matrix3(self.rotation, f"{self.camera_id}.rotation")
        translation = np.asarray(self.translation, dtype=np.float64)
        if translation.shape != (3,):
            raise ValueError(f"{self.camera_id}.translation must have shape (3,)")
        intrinsics = _matrix3(self.intrinsics, f"{self.camera_id}.intrinsics")
        if int(self.image_height) <= 0 or int(self.image_width) <= 0:
            raise ValueError(f"{self.camera_id} image dimensions must be positive")
        object.__setattr__(self, "rotation", rotation)
        object.__setattr__(self, "translation", translation.copy())
        object.__setattr__(self, "intrinsics", intrinsics)
        object.__setattr__(self, "observation_seed", int(self.observation_seed))
        object.__setattr__(self, "image_height", int(self.image_height))
        object.__setattr__(self, "image_width", int(self.image_width))

    @property
    def world_from_camera(self) -> np.ndarray:
        transform = np.eye(4, dtype=np.float64)
        transform[:3, :3] = self.rotation
        transform[:3, 3] = self.translation
        return transform

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CameraPose":
        return cls(
            camera_id=str(value["camera_id"]),
            rotation=value["rotation"],
            translation=value["translation"],
            intrinsics=value["intrinsics"],
            observation_seed=int(value.get("observation_seed", 0)),
            image_height=int(value.get("image_height", 18)),
            image_width=int(value.get("image_width", 24)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "rotation": self.rotation.tolist(),
            "translation": self.translation.tolist(),
            "intrinsics": self.intrinsics.tolist(),
            "observation_seed": int(self.observation_seed),
            "image_height": int(self.image_height),
            "image_width": int(self.image_width),
        }

    @property
    def digest_payload(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "rotation": self.rotation,
            "translation": self.translation,
            "intrinsics": self.intrinsics,
            "observation_seed": int(self.observation_seed),
            "image_height": int(self.image_height),
            "image_width": int(self.image_width),
        }


@dataclass(frozen=True)
class SceneSpec:
    scene_id: str
    scene_revision: int
    camera_poses: tuple[CameraPose, ...]
    point_cloud_seed: int
    texture_seed: int
    geometry_mode: str
    target_face_budget: int
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.scene_id:
            raise ValueError("scene_id cannot be empty")
        if int(self.scene_revision) < 0:
            raise ValueError(f"{self.scene_id}.scene_revision must be >= 0")
        cameras = tuple(self.camera_poses)
        if not cameras:
            raise ValueError(f"{self.scene_id} must have at least one camera")
        camera_ids = [camera.camera_id for camera in cameras]
        if len(camera_ids) != len(set(camera_ids)):
            raise ValueError(f"{self.scene_id} has duplicate camera ids")
        if self.geometry_mode not in VALID_GEOMETRY_MODES:
            raise ValueError(
                f"{self.scene_id}.geometry_mode must be one of {VALID_GEOMETRY_MODES}"
            )
        if int(self.target_face_budget) < 0:
            raise ValueError(f"{self.scene_id}.target_face_budget must be >= 0")
        object.__setattr__(self, "scene_revision", int(self.scene_revision))
        object.__setattr__(self, "camera_poses", cameras)
        object.__setattr__(self, "point_cloud_seed", int(self.point_cloud_seed))
        object.__setattr__(self, "texture_seed", int(self.texture_seed))
        object.__setattr__(self, "target_face_budget", int(self.target_face_budget))
        object.__setattr__(self, "priority", int(self.priority))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SceneSpec":
        required = (
            "scene_id",
            "scene_revision",
            "camera_poses",
            "point_cloud_seed",
            "texture_seed",
            "geometry_mode",
            "target_face_budget",
        )
        missing = [field for field in required if field not in value]
        if missing:
            raise ValueError(f"scene manifest is missing fields: {missing}")
        return cls(
            scene_id=str(value["scene_id"]),
            scene_revision=int(value["scene_revision"]),
            camera_poses=tuple(
                CameraPose.from_dict(camera) for camera in value["camera_poses"]
            ),
            point_cloud_seed=int(value["point_cloud_seed"]),
            texture_seed=int(value["texture_seed"]),
            geometry_mode=str(value["geometry_mode"]),
            target_face_budget=int(value["target_face_budget"]),
            priority=int(value.get("priority", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_revision": int(self.scene_revision),
            "camera_poses": [camera.to_dict() for camera in self.camera_poses],
            "point_cloud_seed": int(self.point_cloud_seed),
            "texture_seed": int(self.texture_seed),
            "geometry_mode": self.geometry_mode,
            "target_face_budget": int(self.target_face_budget),
            "priority": int(self.priority),
        }

    @property
    def camera_pose_digest(self) -> str:
        return content_checksum(
            sorted(
                (camera.digest_payload for camera in self.camera_poses),
                key=lambda item: item["camera_id"],
            )
        )

    @property
    def content_hash(self) -> str:
        # scene_revision is bookkeeping, not computation content. Camera
        # observations are represented by camera_pose_digest and seeds.
        return content_checksum(
            {
                "scene_id": self.scene_id,
                "point_cloud_seed": int(self.point_cloud_seed),
                "texture_seed": int(self.texture_seed),
                "camera_observation_seeds": sorted(
                    (camera.camera_id, int(camera.observation_seed))
                    for camera in self.camera_poses
                ),
            }
        )

    def sorted_cameras(self) -> tuple[CameraPose, ...]:
        return tuple(sorted(self.camera_poses, key=lambda camera: camera.camera_id))


def load_scene_manifest(path: str) -> list[SceneSpec]:
    scenes: list[SceneSpec] = []
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            scene = SceneSpec.from_dict(value)
            if scene.scene_id in seen:
                raise ValueError(f"duplicate scene_id {scene.scene_id!r} in {path}")
            seen.add(scene.scene_id)
            scenes.append(scene)
    scenes.sort(key=lambda scene: scene.scene_id)
    return scenes


def scenes_by_id(scenes: Iterable[SceneSpec]) -> dict[str, SceneSpec]:
    scene_list = list(scenes)
    result = {scene.scene_id: scene for scene in scene_list}
    if len(result) != len(scene_list):
        raise ValueError("duplicate scene ids")
    return result

