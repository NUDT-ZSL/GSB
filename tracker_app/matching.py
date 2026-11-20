from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .errors import TrackingError
from .models import Detection, Track


def iou(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    intersection_width = max(0.0, x2 - x1)
    intersection_height = max(0.0, y2 - y1)
    intersection = intersection_width * intersection_height
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    if union <= 0.0:
        return 0.0
    return intersection / union


def cosine_similarity(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> float:
    dot = sum(a * b for a, b in zip(first, second))
    norm_first = math.sqrt(sum(item * item for item in first))
    norm_second = math.sqrt(sum(item * item for item in second))
    if norm_first <= 0.0 or norm_second <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_first * norm_second)))


def predicted_bbox_at(
    track: Track,
    frame_index: int,
) -> tuple[float, float, float, float]:
    if track.state != "lost":
        return track.last_bbox
    delta = max(0, frame_index - track.last_frame)
    vx, vy, vw, vh = track.velocity
    x1, y1, x2, y2 = track.last_bbox
    return (
        x1 + vx * delta,
        y1 + vy * delta,
        x2 + vx * delta + vw * delta,
        y2 + vy * delta + vh * delta,
    )


class BoundedAppearanceCache:
    def __init__(self, max_entries: int, policy: str) -> None:
        self.max_entries = max_entries
        self.policy = policy
        self._entries: OrderedDict[str, tuple[float, float, float]] = OrderedDict()
        self.evictions = 0
        self.degraded_categories: set[str] = set()

    def get(self, key: str) -> tuple[float, float, float] | None:
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
        return value

    def put(self, key: str, value: tuple[float, float, float]) -> None:
        if key in self._entries:
            self._entries[key] = value
            self._entries.move_to_end(key)
            return
        if len(self._entries) >= self.max_entries:
            if self.policy == "error":
                raise TrackingError(
                    "E_RESOURCE_LIMIT",
                    "appearance feature cache limit exceeded",
                    category="appearance_cache",
                    context={
                        "max_entries": self.max_entries,
                        "resident_entries": len(self._entries),
                    },
                )
            self._entries.popitem(last=False)
            self.evictions += 1
            self.degraded_categories.add("appearance_cache_eviction")
        self._entries[key] = value

    def clear_working_set(self) -> None:
        self._entries.clear()

    @property
    def resident_entries(self) -> int:
        return len(self._entries)


@dataclass(frozen=True)
class Candidate:
    track_id: int
    detection_index: int
    track_id_true: int
    detection_score: float
    iou_value: float
    appearance_similarity: float
    cost: float
    accepted: bool = False
    disposition: str = "gate"

    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.cost,
            self.track_id_true,
            -self.detection_score,
            self.track_id,
            self.detection_index,
        )

    def with_disposition(self, accepted: bool, disposition: str) -> "Candidate":
        return Candidate(
            track_id=self.track_id,
            detection_index=self.detection_index,
            track_id_true=self.track_id_true,
            detection_score=self.detection_score,
            iou_value=self.iou_value,
            appearance_similarity=self.appearance_similarity,
            cost=self.cost,
            accepted=accepted,
            disposition=disposition,
        )


@dataclass
class MatchingResult:
    detection_to_track: dict[int, int]
    candidates: list[Candidate]
    chosen_cost: dict[int, float]


def _appearance_for_detection(
    detection: Detection,
    cache: BoundedAppearanceCache,
) -> tuple[float, float, float]:
    key = "|".join(
        [
            detection.seq_id,
            str(detection.revision),
            str(detection.frame_index),
            str(detection.track_id_true),
            ",".join(f"{item:.6f}" for item in detection.appearance),
        ]
    )
    cached = cache.get(key)
    if cached is None:
        cache.put(key, detection.appearance)
        return detection.appearance
    return cached


def assign_frame(
    *,
    seq_id: str,
    revision: int,
    frame_index: int,
    detections: tuple[Detection, ...],
    tracks: dict[int, Track],
    config: dict[str, Any],
    cache: BoundedAppearanceCache,
) -> MatchingResult:
    tracker_config = config["tracker"]
    iou_weight = float(tracker_config["iou_weight"])
    appearance_weight = float(tracker_config["appearance_weight"])
    gate = float(tracker_config["match_gate"])
    cost_decimals = int(tracker_config["cost_decimals"])

    active_tracks = [
        track
        for track in sorted(tracks.values(), key=lambda item: item.track_id)
        if track.state != "terminated"
    ]
    candidates: list[Candidate] = []
    for detection_index, detection in enumerate(detections):
        detection_appearance = _appearance_for_detection(detection, cache)
        for track in active_tracks:
            predicted = predicted_bbox_at(track, frame_index)
            iou_value = iou(predicted, detection.bbox)
            similarity = cosine_similarity(track.appearance, detection_appearance)
            cost = iou_weight * (1.0 - iou_value) + appearance_weight * (
                1.0 - similarity
            )
            cost = round(cost, cost_decimals)
            if cost > gate:
                disposition = "gate"
            else:
                disposition = "candidate"
            candidates.append(
                Candidate(
                    track_id=track.track_id,
                    detection_index=detection_index,
                    track_id_true=detection.track_id_true,
                    detection_score=detection.score,
                    iou_value=round(iou_value, 10),
                    appearance_similarity=round(similarity, 10),
                    cost=cost,
                    disposition=disposition,
                )
            )

    candidates.sort(key=lambda item: item.sort_key())
    used_tracks: set[int] = set()
    used_detections: set[int] = set()
    detection_to_track: dict[int, int] = {}
    chosen_cost: dict[int, float] = {}
    disposition_by_pair: dict[tuple[int, int], tuple[bool, str]] = {}

    for candidate in candidates:
        if candidate.disposition == "gate":
            disposition_by_pair[
                (candidate.track_id, candidate.detection_index)
            ] = (False, "gate")
            continue
        if candidate.track_id in used_tracks:
            disposition_by_pair[
                (candidate.track_id, candidate.detection_index)
            ] = (False, "track_occupied")
            continue
        if candidate.detection_index in used_detections:
            disposition_by_pair[
                (candidate.track_id, candidate.detection_index)
            ] = (False, "detection_occupied")
            continue
        used_tracks.add(candidate.track_id)
        used_detections.add(candidate.detection_index)
        detection_to_track[candidate.detection_index] = candidate.track_id
        chosen_cost[candidate.detection_index] = candidate.cost
        disposition_by_pair[(candidate.track_id, candidate.detection_index)] = (
            True,
            "assigned",
        )

    annotated = [
        candidate.with_disposition(*disposition_by_pair[(candidate.track_id, candidate.detection_index)])
        for candidate in candidates
    ]
    return MatchingResult(
        detection_to_track=detection_to_track,
        candidates=annotated,
        chosen_cost=chosen_cost,
    )
