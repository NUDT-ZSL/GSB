"""Per-route IoU candidate matching and trajectory maintenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .detection import EPSILON, greater_or_equal, iou


@dataclass
class Track:
    local_id: int
    class_name: str
    created_frame: int
    last_frame: int
    last_bbox: tuple[int, int, int, int]
    last_score: float
    best_score: float
    center: tuple[float, float]
    velocity: tuple[float, float]
    frames: list[dict[str, Any]] = field(default_factory=list)

    def append(self, frame: dict[str, Any]) -> None:
        frame_index = int(frame["frame_index"])
        bbox = tuple(int(value) for value in frame["bbox"])
        score = float(frame["score"])
        new_center = _bbox_center(bbox)
        gap = max(1, frame_index - self.last_frame)
        velocity = (
            round((new_center[0] - self.center[0]) / gap, 6),
            round((new_center[1] - self.center[1]) / gap, 6),
        )
        self.frames.append(
            {
                "frame_index": frame_index,
                "timestamp_ms": round(float(frame["timestamp_ms"]), 6),
                "bbox": list(bbox),
                "score": round(score, 12),
                "stable_key": str(frame["stable_key"]),
            }
        )
        self.last_frame = frame_index
        self.last_bbox = bbox  # type: ignore[assignment]
        self.last_score = round(score, 12)
        self.best_score = max(self.best_score, self.last_score)
        self.center = new_center
        self.velocity = velocity

    def to_state(self) -> dict[str, Any]:
        return {
            "local_id": self.local_id,
            "class_name": self.class_name,
            "created_frame": self.created_frame,
            "last_frame": self.last_frame,
            "last_bbox": list(self.last_bbox),
            "last_score": self.last_score,
            "best_score": self.best_score,
            "center": list(self.center),
            "velocity": list(self.velocity),
            "frames": self.frames,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "Track":
        return cls(
            local_id=int(state["local_id"]),
            class_name=str(state["class_name"]),
            created_frame=int(state["created_frame"]),
            last_frame=int(state["last_frame"]),
            last_bbox=tuple(int(value) for value in state["last_bbox"]),  # type: ignore[arg-type]
            last_score=float(state["last_score"]),
            best_score=float(state["best_score"]),
            center=(float(state["center"][0]), float(state["center"][1])),
            velocity=(float(state["velocity"][0]), float(state["velocity"][1])),
            frames=[dict(frame) for frame in state.get("frames", [])],
        )


class CandidateTracker:
    def __init__(
        self,
        capacities: dict[str, int],
        iou_match_threshold: float,
        tracks: list[Track] | None = None,
    ):
        self.capacities = dict(capacities)
        self.iou_match_threshold = iou_match_threshold
        self.tracks: list[Track] = list(tracks or [])
        self._next_local_id = 1 + max((track.local_id for track in self.tracks), default=0)

    def process_frame(self, frame: dict[str, Any]) -> None:
        frame_index = int(frame["frame_index"])
        candidates = sorted(
            frame.get("candidates", []),
            key=lambda candidate: (-float(candidate["score"]), str(candidate["stable_key"])),
        )
        by_class: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            by_class.setdefault(str(candidate["class_name"]), []).append(candidate)

        for class_name in sorted(by_class):
            class_candidates = by_class[class_name]
            active_tracks = [
                track
                for track in self.tracks
                if track.class_name == class_name and track.last_frame == frame_index - 1
            ]
            pairs: list[tuple[float, int, str, Track, dict[str, Any]]] = []
            for track in active_tracks:
                for candidate in class_candidates:
                    overlap = iou(track.last_bbox, candidate["bbox"])
                    if greater_or_equal(overlap, self.iou_match_threshold):
                        pairs.append(
                            (
                                round(overlap, 12),
                                track.local_id,
                                str(candidate["stable_key"]),
                                track,
                                candidate,
                            )
                        )
            pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
            used_tracks: set[int] = set()
            used_candidates: set[str] = set()
            for _, local_id, stable_key, track, candidate in pairs:
                if local_id in used_tracks or stable_key in used_candidates:
                    continue
                track.append(_frame_candidate(frame, candidate))
                used_tracks.add(local_id)
                used_candidates.add(stable_key)

            unmatched = [
                candidate
                for candidate in class_candidates
                if str(candidate["stable_key"]) not in used_candidates
            ]
            for candidate in unmatched:
                if not self._make_room(class_name, float(candidate["score"])):
                    continue
                self._create_track(frame, candidate)

    def to_state(self) -> list[dict[str, Any]]:
        return [track.to_state() for track in sorted(self.tracks, key=lambda item: item.local_id)]

    @classmethod
    def from_state(
        cls,
        capacities: dict[str, int],
        iou_match_threshold: float,
        state: list[dict[str, Any]],
    ) -> "CandidateTracker":
        return cls(
            capacities,
            iou_match_threshold,
            [Track.from_state(track_state) for track_state in state],
        )

    def _make_room(self, class_name: str, candidate_score: float) -> bool:
        capacity = self.capacities.get(class_name)
        if capacity is None:
            raise ValueError(f"candidate pool capacity is missing class {class_name!r}")
        class_tracks = [track for track in self.tracks if track.class_name == class_name]
        if len(class_tracks) < capacity:
            return True
        # "Oldest low-score" is encoded as lowest score, then oldest last frame, then id.
        evictee = min(
            class_tracks,
            key=lambda track: (track.last_score, track.last_frame, track.local_id),
        )
        if candidate_score <= evictee.last_score + EPSILON:
            return False
        self.tracks.remove(evictee)
        return True

    def _create_track(self, frame: dict[str, Any], candidate: dict[str, Any]) -> None:
        bbox = tuple(int(value) for value in candidate["bbox"])
        score = round(float(candidate["score"]), 12)
        frame_index = int(frame["frame_index"])
        track = Track(
            local_id=self._next_local_id,
            class_name=str(candidate["class_name"]),
            created_frame=frame_index,
            last_frame=frame_index,
            last_bbox=bbox,  # type: ignore[arg-type]
            last_score=score,
            best_score=score,
            center=_bbox_center(bbox),
            velocity=(0.0, 0.0),
            frames=[_frame_candidate(frame, candidate)],
        )
        self._next_local_id += 1
        self.tracks.append(track)


def _frame_candidate(frame: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "frame_index": int(frame["frame_index"]),
        "timestamp_ms": round(float(frame["timestamp_ms"]), 6),
        "bbox": [int(value) for value in candidate["bbox"]],
        "score": round(float(candidate["score"]), 12),
        "stable_key": str(candidate["stable_key"]),
    }


def _bbox_center(bbox: tuple[int, int, int, int]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
