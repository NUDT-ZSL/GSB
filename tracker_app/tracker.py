from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import TrackingError
from .matching import (
    BoundedAppearanceCache,
    MatchingResult,
    assign_frame,
    predicted_bbox_at,
)
from .models import Detection, Track


@dataclass
class FrameProcessResult:
    assignments: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    frame_view: list[dict[str, Any]]
    state_distribution: dict[str, int]
    tracks: dict[int, Track]
    next_track_id: int
    degraded_categories: list[str]


class TrackerStateMachine:
    def __init__(
        self,
        *,
        seq_id: str,
        revision: int,
        config: dict[str, Any],
        tracks: dict[int, Track] | None = None,
        next_track_id: int = 1,
    ) -> None:
        self.seq_id = seq_id
        self.revision = revision
        self.config = config
        self.tracks = tracks or {}
        self.next_track_id = next_track_id
        self.cache = BoundedAppearanceCache(
            int(config["resources"]["max_appearance_cache_entries"]),
            str(config["resources"]["policy"]),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "mapping_namespace": f"{self.seq_id}:r{self.revision}",
            "next_track_id": self.next_track_id,
            "tracks": [
                self.tracks[track_id].to_dict()
                for track_id in sorted(self.tracks)
            ],
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        seq_id: str,
        revision: int,
        config: dict[str, Any],
    ) -> "TrackerStateMachine":
        expected_namespace = f"{seq_id}:r{revision}"
        namespace = snapshot.get("mapping_namespace")
        if namespace != expected_namespace:
            raise TrackingError(
                "E_REVISION_INVALID",
                "checkpoint mapping namespace does not match the requested revision",
                seq_id=seq_id,
                category="mapping_namespace_mismatch",
                context={
                    "expected": expected_namespace,
                    "checkpoint": namespace,
                },
            )
        tracks = {
            int(item["track_id"]): Track.from_dict(item)
            for item in snapshot["tracks"]
        }
        _validate_state_snapshot(seq_id, tracks)
        return cls(
            seq_id=seq_id,
            revision=revision,
            config=config,
            tracks=tracks,
            next_track_id=int(snapshot["next_track_id"]),
        )

    def _apply_hit(
        self,
        track: Track,
        detection: Detection,
        frame_index: int,
    ) -> str:
        if track.state == "terminated":
            raise TrackingError(
                "E_TRACK_ILLEGAL",
                "terminated prediction track cannot be resurrected",
                seq_id=self.seq_id,
                frame_index=frame_index,
                category="terminated_track_resurrection",
                context={"track_id": track.track_id},
            )
        previous_state = track.state
        if previous_state == "lost":
            track.resurrection_count += 1
            event = "resurrected"
        elif previous_state == "new":
            event = "confirmed"
        else:
            event = "matched"

        old_center_x = (track.last_bbox[0] + track.last_bbox[2]) / 2.0
        old_center_y = (track.last_bbox[1] + track.last_bbox[3]) / 2.0
        old_width = track.last_bbox[2] - track.last_bbox[0]
        old_height = track.last_bbox[3] - track.last_bbox[1]
        new_center_x, new_center_y = detection.center
        new_width, new_height = detection.size
        delta = max(1, frame_index - track.last_frame)
        track.velocity = (
            round((new_center_x - old_center_x) / delta, 10),
            round((new_center_y - old_center_y) / delta, 10),
            round((new_width - old_width) / delta, 10),
            round((new_height - old_height) / delta, 10),
        )
        track.last_bbox = detection.bbox
        track.appearance = detection.appearance
        track.last_frame = frame_index
        track.hits += 1
        track.misses = 0
        track.age += delta
        track.state = "tracked"
        track.termination_frame = None
        return event

    def _apply_miss(self, track: Track, frame_index: int) -> str:
        if track.state == "terminated":
            return "terminated"
        track.misses += 1
        track.age += 1
        lost_after = int(self.config["tracker"]["lost_after_misses"])
        terminate_after = int(self.config["tracker"]["terminate_after_misses"])
        event = "missed"
        if track.misses >= terminate_after:
            track.state = "terminated"
            track.termination_frame = frame_index
            event = "terminated"
        elif track.misses >= lost_after:
            if track.state != "lost":
                event = "lost"
            track.state = "lost"
        return event

    def process_frame(
        self,
        detections: tuple[Detection, ...],
        frame_index: int,
    ) -> FrameProcessResult:
        self.cache.clear_working_set()
        matching: MatchingResult = assign_frame(
            seq_id=self.seq_id,
            revision=self.revision,
            frame_index=frame_index,
            detections=detections,
            tracks=self.tracks,
            config=self.config,
            cache=self.cache,
        )
        detection_events: dict[int, str] = {}
        touched_tracks: set[int] = set()
        event_by_track: dict[int, str] = {}
        assigned_track_by_detection = dict(matching.detection_to_track)

        for detection_index, detection in enumerate(detections):
            track_id = assigned_track_by_detection.get(detection_index)
            if track_id is not None:
                track = self.tracks[track_id]
                event = self._apply_hit(track, detection, frame_index)
                detection_events[detection_index] = event
                touched_tracks.add(track_id)
                event_by_track[track_id] = event
                continue

            track_id = self.next_track_id
            self.next_track_id += 1
            track = Track(
                track_id=track_id,
                state="new",
                birth_frame=frame_index,
                last_frame=frame_index,
                last_bbox=detection.bbox,
                appearance=detection.appearance,
                velocity=(0.0, 0.0, 0.0, 0.0),
                hits=1,
                misses=0,
                age=1,
            )
            self.tracks[track_id] = track
            assigned_track_by_detection[detection_index] = track_id
            detection_events[detection_index] = "born"
            touched_tracks.add(track_id)
            event_by_track[track_id] = "born"

        missed_events: dict[int, str] = {}
        for track_id in sorted(self.tracks):
            if track_id in touched_tracks:
                continue
            track = self.tracks[track_id]
            if track.state == "terminated":
                continue
            event = self._apply_miss(track, frame_index)
            missed_events[track_id] = event

        assignments: list[dict[str, Any]] = []
        for detection_index, detection in enumerate(detections):
            track_id = assigned_track_by_detection.get(detection_index)
            track = self.tracks[track_id] if track_id is not None else None
            matched_candidate = next(
                (
                    candidate
                    for candidate in matching.candidates
                    if candidate.detection_index == detection_index
                    and candidate.accepted
                ),
                None,
            )
            assignments.append(
                {
                    "seq_id": self.seq_id,
                    "revision": self.revision,
                    "frame_index": frame_index,
                    "detection_index": detection_index,
                    "track_id_true": detection.track_id_true,
                    "pred_track_id": track_id,
                    "outcome": (
                        "track_birth"
                        if detection_events.get(detection_index) == "born"
                        else "matched"
                    ),
                    "bbox": list(detection.bbox),
                    "score": detection.score,
                    "class_id": detection.class_id,
                    "occluded": detection.occluded,
                    "iou": matched_candidate.iou_value if matched_candidate else None,
                    "appearance_similarity": (
                        matched_candidate.appearance_similarity
                        if matched_candidate
                        else None
                    ),
                    "cost": matched_candidate.cost if matched_candidate else None,
                    "track_state_after": track.state if track else None,
                    "track_event": detection_events[detection_index],
                    "is_completion": False,
                }
            )

        candidate_rows = [
            {
                "seq_id": self.seq_id,
                "revision": self.revision,
                "frame_index": frame_index,
                "pred_track_id": candidate.track_id,
                "detection_index": candidate.detection_index,
                "track_id_true": candidate.track_id_true,
                "detection_score": candidate.detection_score,
                "iou": candidate.iou_value,
                "appearance_similarity": candidate.appearance_similarity,
                "cost": candidate.cost,
                "accepted": candidate.accepted,
                "disposition": candidate.disposition,
            }
            for candidate in matching.candidates
        ]

        frame_view: list[dict[str, Any]] = []
        distribution = {"new": 0, "tracked": 0, "lost": 0, "terminated": 0}
        for track_id in sorted(self.tracks):
            track = self.tracks[track_id]
            distribution[track.state] += 1
            event: str | None = None
            if track_id in touched_tracks:
                event = event_by_track.get(track_id)
            elif track_id in missed_events:
                event = missed_events[track_id]
            predicted = predicted_bbox_at(track, frame_index)
            frame_view.append(
                {
                    "seq_id": self.seq_id,
                    "revision": self.revision,
                    "frame_index": frame_index,
                    "track_id": track.track_id,
                    "state": track.state,
                    "birth_frame": track.birth_frame,
                    "last_frame": track.last_frame,
                    "hits": track.hits,
                    "misses": track.misses,
                    "age": track.age,
                    "resurrection_count": track.resurrection_count,
                    "termination_frame": track.termination_frame,
                    "state_distribution": None,
                    "predicted_bbox": [round(value, 6) for value in predicted],
                    "event": event,
                }
            )
        for view_row in frame_view:
            view_row["state_distribution"] = dict(distribution)

        return FrameProcessResult(
            assignments=assignments,
            candidates=candidate_rows,
            frame_view=frame_view,
            state_distribution=distribution,
            tracks=self.tracks,
            next_track_id=self.next_track_id,
            degraded_categories=sorted(self.cache.degraded_categories),
        )


def _validate_state_snapshot(seq_id: str, tracks: dict[int, Track]) -> None:
    for track_id, track in tracks.items():
        if track.track_id != track_id:
            raise TrackingError(
                "E_CHECKPOINT_CORRUPT",
                "track map key and track id differ",
                seq_id=seq_id,
                category="track_id_mismatch",
                context={"map_key": track_id, "track_id": track.track_id},
            )
        if track.state not in {"new", "tracked", "lost", "terminated"}:
            raise TrackingError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint contains an unknown track state",
                seq_id=seq_id,
                category="unknown_state",
                context={"track_id": track_id, "state": track.state},
            )
        if track.state == "terminated" and track.termination_frame is None:
            raise TrackingError(
                "E_CHECKPOINT_CORRUPT",
                "terminated track has no termination frame",
                seq_id=seq_id,
                category="missing_termination_frame",
                context={"track_id": track_id},
            )
