"""Scene manifest (JSONL) data model and deterministic ordering helpers."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Sequence


@dataclass(frozen=True)
class CameraSpec:
    camera_id: str
    # World-to-camera 4x4 extrinsic matrix used as the ICP initial guess.
    extrinsic: List[List[float]]
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CameraSpec":
        return cls(**data)


@dataclass(frozen=True)
class SceneManifest:
    scene_id: str
    scene_revision: int
    camera_poses: List[CameraSpec]
    point_cloud_seed: int
    texture_seed: int
    geometry_mode: str  # "solid" | "shell"
    target_face_budget: int
    priority: int

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SceneManifest":
        return cls(
            scene_id=data["scene_id"],
            scene_revision=int(data["scene_revision"]),
            camera_poses=[CameraSpec.from_dict(c) for c in data["camera_poses"]],
            point_cloud_seed=int(data["point_cloud_seed"]),
            texture_seed=int(data["texture_seed"]),
            geometry_mode=str(data["geometry_mode"]),
            target_face_budget=int(data["target_face_budget"]),
            priority=int(data["priority"]),
        )


def write_manifest(path: str, manifests: Sequence[SceneManifest]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for manifest in manifests:
            handle.write(json.dumps(manifest.to_dict(), sort_keys=True))
            handle.write("\n")


def read_manifest(path: str) -> List[SceneManifest]:
    manifests: List[SceneManifest] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                manifests.append(SceneManifest.from_dict(json.loads(line)))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Malformed manifest line {line_number}: {exc}"
                ) from exc
    return manifests


def priority_order(manifests: Sequence[SceneManifest]) -> List[SceneManifest]:
    """Explicit, reproducible arbitration order.

    Higher ``priority`` first; equal priority broken by ``scene_id`` ascending.
    """
    return sorted(manifests, key=lambda m: (-m.priority, m.scene_id))
