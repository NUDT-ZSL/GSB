from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


STATES = ("new", "tracked", "lost", "terminated")


@dataclass(frozen=True)
class Detection:
    seq_id: str
    revision: int
    frame_index: int
    track_id_true: int
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    class_id: int
    occluded: bool
    appearance: tuple[float, float, float]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def size(self) -> tuple[float, float]:
        return (self.x2 - self.x1, self.y2 - self.y1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq_id": self.seq_id,
            "revision": self.revision,
            "frame_index": self.frame_index,
            "track_id_true": self.track_id_true,
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "score": self.score,
            "class_id": self.class_id,
            "occluded": self.occluded,
            "appearance": list(self.appearance),
        }

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        default_revision: int | None = None,
        fallback_appearance: Iterable[float] | None = None,
    ) -> "Detection":
        revision = int(value.get("revision", default_revision or 1))
        appearance_raw = value.get("appearance")
        if appearance_raw is None:
            appearance_raw = list(fallback_appearance or (0.5, 0.5, 0.5))
        appearance = tuple(float(item) for item in appearance_raw)
        if len(appearance) != 3:
            raise ValueError("appearance must have three components")
        return cls(
            seq_id=str(value["seq_id"]),
            revision=revision,
            frame_index=int(value["frame_index"]),
            track_id_true=int(value["track_id_true"]),
            x1=float(value["x1"]),
            y1=float(value["y1"]),
            x2=float(value["x2"]),
            y2=float(value["y2"]),
            score=float(value["score"]),
            class_id=int(value["class_id"]),
            occluded=value["occluded"],
            appearance=appearance,
        )


@dataclass
class Track:
    track_id: int
    state: str
    birth_frame: int
    last_frame: int
    last_bbox: tuple[float, float, float, float]
    appearance: tuple[float, float, float]
    velocity: tuple[float, float, float, float]
    hits: int = 1
    misses: int = 0
    age: int = 1
    resurrection_count: int = 0
    termination_frame: int | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "state": self.state,
            "birth_frame": self.birth_frame,
            "last_frame": self.last_frame,
            "last_bbox": list(self.last_bbox),
            "appearance": list(self.appearance),
            "velocity": list(self.velocity),
            "hits": self.hits,
            "misses": self.misses,
            "age": self.age,
            "resurrection_count": self.resurrection_count,
            "termination_frame": self.termination_frame,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Track":
        return cls(
            track_id=int(value["track_id"]),
            state=str(value["state"]),
            birth_frame=int(value["birth_frame"]),
            last_frame=int(value["last_frame"]),
            last_bbox=tuple(float(item) for item in value["last_bbox"]),
            appearance=tuple(float(item) for item in value["appearance"]),
            velocity=tuple(float(item) for item in value["velocity"]),
            hits=int(value["hits"]),
            misses=int(value["misses"]),
            age=int(value["age"]),
            resurrection_count=int(value.get("resurrection_count", 0)),
            termination_frame=value.get("termination_frame"),
        )

    def predicted_bbox(self) -> tuple[float, float, float, float]:
        if self.state != "lost" or not any(abs(item) > 1e-12 for item in self.velocity):
            return self.last_bbox
        vx, vy, vw, vh = self.velocity
        x1, y1, x2, y2 = self.last_bbox
        return (x1 + vx, y1 + vy, x2 + vx + vw, y2 + vy + vh)
