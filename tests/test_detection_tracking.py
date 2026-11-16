from __future__ import annotations

import numpy as np
import pytest

from video_pipeline.config import DetectionConfig, WindowSpec
from video_pipeline.detection import SlidingWindowDetector, greater_or_equal, iou
from video_pipeline.tracking import CandidateTracker


def make_detection_config(nms_threshold: float = 0.12) -> DetectionConfig:
    return DetectionConfig(
        windows=(
            WindowSpec("vehicle", "vehicle", 56, 36, 8, 0),
            WindowSpec("vehicle-wide", "vehicle", 72, 48, 12, 1),
        ),
        target_colors={"vehicle": (20, 20, 220)},
        color_tolerance=42,
        color_ratio_threshold=0.2,
        edge_pixel_threshold=55.0,
        edge_density_threshold=0.003,
        color_weight=0.65,
        edge_weight=0.35,
        detection_threshold=0.17,
        nms_iou_threshold=nms_threshold,
        source={},
    )


def test_iou_and_threshold_boundary_are_epsilon_safe() -> None:
    assert iou((0, 0, 10, 10), (0, 0, 5, 10)) == pytest.approx(0.5)
    assert greater_or_equal(0.5, 0.5)
    assert greater_or_equal(0.5 - 1e-12, 0.5)
    detector = SlidingWindowDetector(make_detection_config(nms_threshold=0.5))
    candidate_a = {
        "bbox": (0, 0, 10, 10),
        "score": 0.9,
        "stable_key": "a",
        "class_name": "vehicle",
    }
    candidate_b = {
        "bbox": (0, 0, 5, 10),
        "score": 0.8,
        "stable_key": "b",
        "class_name": "vehicle",
    }
    # NMS suppresses only IoU strictly greater than the configured threshold.
    assert len(detector._nms([_candidate(candidate_a), _candidate(candidate_b)])) == 2


def test_sliding_window_detector_returns_integer_clipped_non_nms_boxes() -> None:
    image = np.full((100, 120, 3), 34, dtype=np.uint8)
    image[:, :, 1] = 38
    image[:, :, 2] = 42
    image[20:50, 20:68] = (20, 20, 220)
    detector = SlidingWindowDetector(make_detection_config())
    candidates = detector.detect_frame(image, "cam.avi", 0)
    assert candidates
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["class_name"] == "vehicle"
    assert all(isinstance(value, int) for value in candidate["bbox"])
    x1, y1, x2, y2 = candidate["bbox"]
    assert 0 <= x1 < x2 <= 120
    assert 0 <= y1 < y2 <= 100
    assert 0.0 <= candidate["score"] <= 1.0
    assert candidate["stable_key"].startswith("cam.avi|0000000000|")


def test_tracker_matches_consecutive_frame_and_updates_center_velocity() -> None:
    tracker = CandidateTracker({"vehicle": 2}, iou_match_threshold=0.3)
    tracker.process_frame(_frame(0, [(0, 0, 10, 10, 0.8, "k0")]))
    first_id = tracker.tracks[0].local_id
    tracker.process_frame(_frame(1, [(2, 0, 12, 10, 0.9, "k1")]))
    assert len(tracker.tracks) == 1
    track = tracker.tracks[0]
    assert track.local_id == first_id
    assert track.center == (7.0, 5.0)
    assert track.velocity == (2.0, 0.0)
    assert [frame["frame_index"] for frame in track.frames] == [0, 1]


def test_tracker_pool_evicts_oldest_low_score_and_is_deterministic() -> None:
    tracker = CandidateTracker({"vehicle": 1}, iou_match_threshold=0.3)
    tracker.process_frame(_frame(0, [(0, 0, 10, 10, 0.8, "k0")]))
    first_id = tracker.tracks[0].local_id
    tracker.process_frame(_frame(3, [(100, 100, 110, 110, 0.7, "k1")]))
    assert [track.local_id for track in tracker.tracks] == [first_id]
    tracker.process_frame(_frame(4, [(200, 200, 210, 210, 0.95, "k2")]))
    assert len(tracker.tracks) == 1
    assert tracker.tracks[0].local_id == first_id + 1
    assert tracker.tracks[0].last_score == 0.95


def _frame(index: int, candidates: list[tuple[int, int, int, int, float, str]]) -> dict:
    return {
        "frame_index": index,
        "timestamp_ms": index * 100.0,
        "candidates": [
            {
                "class_name": "vehicle",
                "bbox": [x1, y1, x2, y2],
                "score": score,
                "stable_key": key,
            }
            for x1, y1, x2, y2, score, key in candidates
        ],
    }


def _candidate(item: dict):
    from video_pipeline.detection import Candidate

    return Candidate(
        class_name=item["class_name"],
        bbox=item["bbox"],
        score=item["score"],
        stable_key=item["stable_key"],
        window_id="test",
        window_index=0,
    )
