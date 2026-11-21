"""In-memory data contracts for clips, observations and generated frames."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .canonical import checksum, r6

Box = tuple[float, float, float, float]


def _box_to_list(box: Box) -> list[float]:
    return [r6(value) for value in box]


@dataclass(slots=True)
class ClipSpec:
    clip_id: str
    frame_count: int
    frame_interval_ms: int
    object_count: int
    motion_mode: str
    seed: int
    temporal_policy: str
    model_revision: str
    canvas_height: int = 96
    canvas_width: int = 128

    def to_manifest_dict(self, truth_path: str | None = None, truth_checksum: str | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "clip_id": self.clip_id,
            "frame_count": self.frame_count,
            "frame_interval_ms": self.frame_interval_ms,
            "object_count": self.object_count,
            "motion_mode": self.motion_mode,
            "seed": self.seed,
            "temporal_policy": self.temporal_policy,
            "model_revision": self.model_revision,
            "canvas_height": self.canvas_height,
            "canvas_width": self.canvas_width,
        }
        if truth_path is not None:
            record["truth_path"] = truth_path
        if truth_checksum is not None:
            record["truth_checksum"] = truth_checksum
        return record

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClipSpec":
        return cls(
            clip_id=str(value["clip_id"]),
            frame_count=int(value["frame_count"]),
            frame_interval_ms=int(value["frame_interval_ms"]),
            object_count=int(value["object_count"]),
            motion_mode=str(value["motion_mode"]),
            seed=int(value["seed"]),
            temporal_policy=str(value["temporal_policy"]),
            model_revision=str(value["model_revision"]),
            canvas_height=int(value.get("canvas_height", 96)),
            canvas_width=int(value.get("canvas_width", 128)),
        )

    def canonical_dict(self) -> dict[str, Any]:
        return self.to_manifest_dict()


@dataclass(slots=True)
class TargetObservation:
    object_id: int
    box: Box
    occluded: bool = False
    mask_shape: tuple[int, int] = (96, 128)
    mask_coords: tuple[tuple[int, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        coords = [[int(y), int(x)] for y, x in self.mask_coords]
        return {
            "object_id": self.object_id,
            "box": _box_to_list(self.box),
            "occluded": self.occluded,
            "mask_shape": [self.mask_shape[0], self.mask_shape[1]],
            "mask_coords": coords,
            "mask_summary": {
                "shape": [self.mask_shape[0], self.mask_shape[1]],
                "pixel_count": len(coords),
                "checksum": checksum(coords),
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TargetObservation":
        box_values = value["box"]
        shape = value.get("mask_shape", [96, 128])
        return cls(
            object_id=int(value["object_id"]),
            box=(float(box_values[0]), float(box_values[1]), float(box_values[2]), float(box_values[3])),
            occluded=bool(value.get("occluded", False)),
            mask_shape=(int(shape[0]), int(shape[1])),
            mask_coords=tuple((int(y), int(x)) for y, x in value.get("mask_coords", [])),
        )


@dataclass(slots=True)
class FrameSample:
    clip_id: str
    frame_index: int
    timestamp_ms: int
    observed: bool
    targets: tuple[TargetObservation, ...] = ()
    image: np.ndarray | None = field(default=None, repr=False, compare=False)

    def truth_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "frame_index": self.frame_index,
            "timestamp_ms": self.timestamp_ms,
            "observed": self.observed,
            "targets": [target.to_dict() for target in self.targets],
        }

    @classmethod
    def from_truth_dict(cls, value: dict[str, Any], image: np.ndarray | None = None) -> "FrameSample":
        return cls(
            clip_id=str(value["clip_id"]),
            frame_index=int(value["frame_index"]),
            timestamp_ms=int(value["timestamp_ms"]),
            observed=bool(value["observed"]),
            targets=tuple(TargetObservation.from_dict(item) for item in value.get("targets", [])),
            image=image,
        )


@dataclass(slots=True)
class GeneratedClip:
    spec: ClipSpec
    frames: list[FrameSample]

    def by_frame(self) -> dict[int, FrameSample]:
        return {frame.frame_index: frame for frame in self.frames}


def center(box: Box) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)

