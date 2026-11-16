from __future__ import annotations

import json
import threading
from pathlib import Path

from video_pipeline.atomic_io import atomic_write_json, read_json
from video_pipeline.config import load_detection_config, load_matching_config
from video_pipeline.pipeline import Pipeline
from video_pipeline.samples import create_invalid_samples, generate_sample_corpus
from video_pipeline.tracking import CandidateTracker
from video_pipeline.video import scan_videos

from conftest import FULL_MATCHING_CONFIG, MODEL_CONFIG


def test_atomic_json_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "report.json"
    atomic_write_json(path, {"ok": True})
    assert read_json(path) == {"ok": True}
    assert list(path.parent.glob("*.tmp")) == []
    assert list(path.parent.glob(".*.tmp")) == []


def test_scan_and_decode_failures_do_not_abort_batch(tmp_path: Path) -> None:
    ground_truth = generate_sample_corpus(tmp_path)
    assert len(ground_truth["routes"]) == 4
    invalid_paths = create_invalid_samples(tmp_path)
    assert {path.name for path in invalid_paths} == {"bad_header.avi", "truncated.avi"}
    infos = scan_videos(tmp_path)
    assert any(not info.valid and "header" in (info.error_message or "") for info in infos)
    assert any(info.valid and info.route == "truncated.avi" for info in infos)

    detection = load_detection_config(MODEL_CONFIG)
    matching = load_matching_config(FULL_MATCHING_CONFIG)
    result = Pipeline(
        tmp_path,
        detection,
        matching,
        tmp_path / "report.json",
        tmp_path / "state",
        resume=False,
        stop_event=threading.Event(),
    ).run()
    failures = result.report["failures"]
    decode_failures = [failure for failure in failures if failure["code"] == "E_DECODE_FAIL"]
    assert any(failure["route"] == "bad_header.avi" and failure["frame_index"] == 0 for failure in decode_failures)
    assert any(failure["route"] == "truncated.avi" and failure["frame_index"] is not None for failure in decode_failures)
    assert result.report["tracks"]


def test_detection_and_matching_exceptions_are_isolated_per_frame(corpus_dir: Path, tmp_path: Path) -> None:
    detection_config = load_detection_config(MODEL_CONFIG)
    matching_config = load_matching_config(FULL_MATCHING_CONFIG)
    from video_pipeline.detection import SlidingWindowDetector

    base_detector = SlidingWindowDetector(detection_config)

    class FailingDetector:
        def detect_frame(self, image, route, frame_index):
            if route == "cam_rear.avi" and frame_index == 2:
                raise RuntimeError("injected detector failure")
            return base_detector.detect_frame(image, route, frame_index)

    state = {"rear_match_failed": False}

    def tracker_factory(route: str) -> CandidateTracker:
        tracker = CandidateTracker(
            matching_config.candidate_pool_capacity,
            matching_config.iou_match_threshold,
        )
        original = tracker.process_frame

        def process_frame(frame):
            if (
                route == "cam_rear.avi"
                and frame["frame_index"] == 3
                and not state["rear_match_failed"]
            ):
                state["rear_match_failed"] = True
                raise RuntimeError("injected matching failure")
            original(frame)

        tracker.process_frame = process_frame  # type: ignore[method-assign]
        return tracker

    result = Pipeline(
        corpus_dir,
        detection_config,
        matching_config,
        tmp_path / "report.json",
        tmp_path / "state",
        resume=False,
        stop_event=threading.Event(),
        detector=FailingDetector(),  # type: ignore[arg-type]
        tracker_factory=tracker_factory,
    ).run()
    failures = result.report["failures"]
    detection_failure = next(failure for failure in failures if failure["code"] == "E_DETECTION_FAIL")
    matching_failure = next(failure for failure in failures if failure["code"] == "E_MATCHING_FAIL")
    assert detection_failure["route"] == "cam_rear.avi"
    assert detection_failure["frame_index"] == 2
    assert detection_failure["stage"] == "detection"
    assert matching_failure["route"] == "cam_rear.avi"
    assert matching_failure["frame_index"] == 3
    assert matching_failure["stage"] == "matching"
    assert matching_failure["exception_type"] == "RuntimeError"
    route_report = next(item for item in result.report["routes"] if item["route"] == "cam_front.avi")
    assert route_report["processed_frames"] == route_report["source_frame_count"]
    assert result.report["tracks"]
