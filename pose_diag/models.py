"""Core immutable records shared by every pipeline stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .io_utils import round_float


@dataclass(frozen=True)
class SceneRecord:
    scene_id: str
    scene_seed: int
    canvas_shape: tuple[int, int, int]
    person_count: int
    num_keypoints: int
    heatmap_seed: int
    occlusion_ratio: float
    policy: str
    sample_dir: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SceneRecord:
        shape = raw.get("canvas_shape")
        if isinstance(shape, list):
            shape = tuple(shape)
        return cls(
            scene_id=str(raw.get("scene_id", "")),
            scene_seed=int(raw.get("scene_seed", 0)),
            canvas_shape=shape,  # type: ignore[arg-type]
            person_count=int(raw.get("person_count", 0)),
            num_keypoints=int(raw.get("num_keypoints", 0)),
            heatmap_seed=int(raw.get("heatmap_seed", 0)),
            occlusion_ratio=float(raw.get("occlusion_ratio", 0.0)),
            policy=str(raw.get("policy", "")),
            sample_dir=str(raw.get("sample_dir", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_seed": self.scene_seed,
            "canvas_shape": list(self.canvas_shape),
            "person_count": self.person_count,
            "num_keypoints": self.num_keypoints,
            "heatmap_seed": self.heatmap_seed,
            "occlusion_ratio": self.occlusion_ratio,
            "policy": self.policy,
            "sample_dir": self.sample_dir,
        }


@dataclass(frozen=True)
class RegionSpec:
    row: int
    col: int
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def pixel_count(self) -> int:
        return max(0, self.x1 - self.x0) * max(0, self.y1 - self.y0)

    @property
    def region_id(self) -> str:
        return f"r{self.row:02d}c{self.col:02d}"

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "row": self.row,
            "col": self.col,
            "bbox": [self.x0, self.y0, self.x1, self.y1],
            "pixel_count": self.pixel_count,
        }


@dataclass(frozen=True)
class Peak:
    channel: int
    y: int
    x: int
    intensity: float
    refined_y: float
    refined_x: float
    window_clipped: bool

    @property
    def sort_key(self) -> tuple[float, int, int, int]:
        return (-self.intensity, self.channel, self.y, self.x)

    def to_dict(self, decimals: int = 9) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "y": self.y,
            "x": self.x,
            "intensity": round_float(self.intensity, decimals),
            "refined_y": round_float(self.refined_y, 12),
            "refined_x": round_float(self.refined_x, 12),
            "window_clipped": self.window_clipped,
        }

    def canonical(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass
class Instance:
    instance_id: str
    root_channel: int
    peaks: tuple[Peak, ...]
    score: float = 0.0
    matched: bool = False
    cache_key: str = ""
    snapshot_versions: dict[str, int] = field(default_factory=dict)
    snapshot_checksums: dict[str, str] = field(default_factory=dict)
    node_path: list[str] = field(default_factory=list)

    def keypoint_dicts(self) -> list[dict[str, Any]]:
        return [peak.to_dict() for peak in sorted(self.peaks, key=lambda item: item.channel)]

    def canonical(self, score_decimals: int = 9) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "keypoints": self.keypoint_dicts(),
            "score": round_float(self.score, score_decimals),
            "matched": bool(self.matched),
        }

    def record(self, score_decimals: int = 9) -> dict[str, Any]:
        payload = self.canonical(score_decimals)
        payload.update(
            {
                "cache_key": self.cache_key,
                "snapshot_versions": dict(sorted(self.snapshot_versions.items())),
                "snapshot_checksums": dict(sorted(self.snapshot_checksums.items())),
                "node_path": list(self.node_path),
            }
        )
        return payload

    def keypoint_set(self) -> set[tuple[int, int, int]]:
        return {(peak.channel, peak.y, peak.x) for peak in self.peaks}


@dataclass
class SceneSample:
    record: SceneRecord
    canvas: np.ndarray
    truth_heatmaps: np.ndarray
    observation_heatmaps: np.ndarray
    metadata: dict[str, Any]
    scoring: dict[str, Any]

