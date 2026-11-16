from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
from pathlib import Path

from video_pipeline.config import load_detection_config, load_matching_config
from video_pipeline.detection import SlidingWindowDetector, iou
from video_pipeline.pipeline import Pipeline
from video_pipeline.tracking import CandidateTracker

from conftest import (
    BUDGET_MATCHING_CONFIG,
    FULL_MATCHING_CONFIG,
    MODEL_CONFIG,
    PROJECT_ROOT,
    SERIAL_MATCHING_CONFIG,
)


def test_parallel_and_serial_outputs_are_field_identical(corpus_dir: Path, tmp_path: Path) -> None:
    detection = load_detection_config(MODEL_CONFIG)
    parallel_matching = load_matching_config(FULL_MATCHING_CONFIG)
    serial_matching = load_matching_config(SERIAL_MATCHING_CONFIG)
    parallel = Pipeline(
        corpus_dir,
        detection,
        parallel_matching,
        tmp_path / "parallel.json",
        tmp_path / "parallel-state",
        resume=False,
    ).run()
    serial = Pipeline(
        corpus_dir,
        detection,
        serial_matching,
        tmp_path / "serial.json",
        tmp_path / "serial-state",
        resume=False,
    ).run()

    assert parallel.report["tracks"] == serial.report["tracks"]
    assert parallel.report["routes"] == serial.report["routes"]
    assert parallel.report["failures"] == serial.report["failures"]
    assert parallel.report["config_hash"] == serial.report["config_hash"]
    assert parallel.report["result_hash"] == serial.report["result_hash"]
    assert 1 <= serial.report["budget"]["peak_workers"] <= 1
    assert 1 <= parallel.report["budget"]["peak_workers"] <= parallel_matching.max_workers

    snapshot_detection = json.loads((tmp_path / "parallel-state" / "detection_snapshot.json").read_text(encoding="utf-8"))
    snapshot_matching = json.loads((tmp_path / "parallel-state" / "matching_snapshot.json").read_text(encoding="utf-8"))
    assert snapshot_detection["stage"] == "detection"
    assert snapshot_detection["config_hash"] == parallel.report["config_hash"]
    assert snapshot_detection["frames_processed"] == 62
    assert snapshot_matching["stage"] == "matching"
    assert any(route["tracks"] for route in snapshot_matching["routes"].values())

    _assert_ground_truth_coverage(parallel.report, corpus_dir / "ground_truth.json")
    track_ids = [track["track_id"] for track in parallel.report["tracks"]]
    assert track_ids == sorted(track_ids)
    assert len(track_ids) == len(set(track_ids))


def test_budget_exhaustion_truncates_by_priority_without_overread(corpus_dir: Path, tmp_path: Path) -> None:
    detection = load_detection_config(MODEL_CONFIG)
    matching = load_matching_config(BUDGET_MATCHING_CONFIG)
    result = Pipeline(
        corpus_dir,
        detection,
        matching,
        tmp_path / "budget.json",
        tmp_path / "budget-state",
        resume=False,
    ).run()
    routes = {route["route"]: route for route in result.report["routes"]}
    assert result.report["budget"]["frames_processed"] == 36
    assert result.report["budget"]["allocated_frames"] == 36
    assert routes["cam_front.avi"]["allocated_frames"] == 14
    assert routes["cam_rear.avi"]["allocated_frames"] == 12
    assert routes["cam_short.avi"]["allocated_frames"] == 8
    assert routes["cam_side.avi"]["allocated_frames"] == 2
    assert routes["cam_short.avi"]["status"] == "completed"
    assert routes["cam_front.avi"]["truncated_frame"] == 14
    assert routes["cam_rear.avi"]["truncated_frame"] == 12
    assert routes["cam_side.avi"]["truncated_frame"] == 2
    budget_failures = [failure for failure in result.report["failures"] if failure["code"] == "E_BUDGET_EXHAUSTED"]
    assert {(failure["route"], failure["frame_index"]) for failure in budget_failures} == {
        ("cam_front.avi", 14),
        ("cam_rear.avi", 12),
        ("cam_side.avi", 2),
    }
    assert all(
        route["processed_frames"] == route["allocated_frames"]
        for route in result.report["routes"]
    )


def test_detection_and_matching_resume_match_uninterrupted_run(corpus_dir: Path, tmp_path: Path) -> None:
    detection_config = load_detection_config(MODEL_CONFIG)
    matching_config = load_matching_config(SERIAL_MATCHING_CONFIG)
    base_detector = SlidingWindowDetector(detection_config)

    uninterrupted = Pipeline(
        corpus_dir,
        detection_config,
        matching_config,
        tmp_path / "uninterrupted.json",
        tmp_path / "uninterrupted-state",
        resume=False,
    ).run()

    detection_stop = threading.Event()

    class StopDetector:
        def __init__(self) -> None:
            self.calls = 0

        def detect_frame(self, image, route, frame_index):
            self.calls += 1
            output = base_detector.detect_frame(image, route, frame_index)
            if self.calls >= 5:
                detection_stop.set()
            return output

    partial_detection = Pipeline(
        corpus_dir,
        detection_config,
        matching_config,
        tmp_path / "partial-detection.json",
        tmp_path / "detection-resume-state",
        resume=True,
        stop_event=detection_stop,
        detector=StopDetector(),  # type: ignore[arg-type]
    ).run()
    assert partial_detection.report["status"] == "graceful_stop"
    assert partial_detection.report["budget"]["frames_processed"] == 5
    resumed_detection = Pipeline(
        corpus_dir,
        detection_config,
        matching_config,
        tmp_path / "resumed-detection.json",
        tmp_path / "detection-resume-state",
        resume=True,
    ).run()
    assert resumed_detection.report["result_hash"] == uninterrupted.report["result_hash"]
    assert resumed_detection.report["tracks"] == uninterrupted.report["tracks"]
    assert resumed_detection.report["recovery"]["detection_snapshot_loaded"] is True

    matching_stop = threading.Event()
    counter = {"calls": 0}

    def stop_tracker_factory(route: str) -> CandidateTracker:
        del route
        tracker = CandidateTracker(
            matching_config.candidate_pool_capacity,
            matching_config.iou_match_threshold,
        )
        original = tracker.process_frame

        def process_frame(frame):
            original(frame)
            counter["calls"] += 1
            if counter["calls"] >= 10:
                matching_stop.set()

        tracker.process_frame = process_frame  # type: ignore[method-assign]
        return tracker

    partial_matching = Pipeline(
        corpus_dir,
        detection_config,
        matching_config,
        tmp_path / "partial-matching.json",
        tmp_path / "matching-resume-state",
        resume=True,
        stop_event=matching_stop,
        tracker_factory=stop_tracker_factory,
    ).run()
    assert partial_matching.report["status"] == "graceful_stop"
    assert partial_matching.report["budget"]["frames_processed"] == 62
    assert any(route["matched_frames"] < route["processed_frames"] for route in partial_matching.report["routes"])
    resumed_matching = Pipeline(
        corpus_dir,
        detection_config,
        matching_config,
        tmp_path / "resumed-matching.json",
        tmp_path / "matching-resume-state",
        resume=True,
    ).run()
    assert resumed_matching.report["result_hash"] == uninterrupted.report["result_hash"]
    assert resumed_matching.report["tracks"] == uninterrupted.report["tracks"]
    assert resumed_matching.report["recovery"]["matching_snapshot_loaded"] is True


def test_snapshot_config_mismatch_rejects_restore(corpus_dir: Path, tmp_path: Path) -> None:
    detection = load_detection_config(MODEL_CONFIG)
    matching = load_matching_config(FULL_MATCHING_CONFIG)
    state_dir = tmp_path / "state"
    Pipeline(
        corpus_dir,
        detection,
        matching,
        tmp_path / "first.json",
        state_dir,
        resume=False,
    ).run()

    changed_payload = json.loads(FULL_MATCHING_CONFIG.read_text(encoding="utf-8"))
    changed_payload["iou_match_threshold"] = 0.55
    changed_path = tmp_path / "changed-matching.json"
    changed_path.write_text(json.dumps(changed_payload), encoding="utf-8")
    changed = load_matching_config(changed_path)
    result = Pipeline(
        corpus_dir,
        detection,
        changed,
        tmp_path / "mismatch.json",
        state_dir,
        resume=True,
    ).run()
    mismatches = [failure for failure in result.report["failures"] if failure["code"] == "E_CONFIG_MISMATCH"]
    assert len(mismatches) == 1
    assert mismatches[0]["stage"] == "snapshot"
    assert result.report["recovery"]["detection_snapshot_loaded"] is False
    assert result.report["recovery"]["matching_snapshot_loaded"] is False
    assert result.report["tracks"]


def test_stop_before_run_still_writes_graceful_budget_report(corpus_dir: Path, tmp_path: Path) -> None:
    detection = load_detection_config(MODEL_CONFIG)
    matching = load_matching_config(FULL_MATCHING_CONFIG)
    stop_event = threading.Event()
    stop_event.set()
    result = Pipeline(
        corpus_dir,
        detection,
        matching,
        tmp_path / "early-stop.json",
        tmp_path / "early-stop-state",
        resume=False,
        stop_event=stop_event,
    ).run()
    assert result.report["status"] == "graceful_stop"
    assert result.report["exit_reason"] == "graceful_signal"
    assert result.report["budget"]["frames_processed"] == 0
    assert all(route["status"] == "graceful_stop" for route in result.report["routes"])
    assert (tmp_path / "early-stop.json").exists()


def test_route_config_error_is_isolated_and_other_routes_continue(corpus_dir: Path, tmp_path: Path) -> None:
    detection = load_detection_config(MODEL_CONFIG)
    payload = json.loads(FULL_MATCHING_CONFIG.read_text(encoding="utf-8"))
    for route in payload["routes"]:
        if route["name"] == "cam_short.avi":
            route.pop("frame_budget")
            break
    path = tmp_path / "route-invalid-matching.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    matching = load_matching_config(path)
    result = Pipeline(
        corpus_dir,
        detection,
        matching,
        tmp_path / "route-invalid.json",
        tmp_path / "route-invalid-state",
        resume=False,
    ).run()
    config_failures = [failure for failure in result.report["failures"] if failure["code"] == "E_CONFIG_INVALID"]
    assert len(config_failures) == 1
    assert config_failures[0]["route"] == "cam_short.avi"
    assert config_failures[0]["stage"] == "config"
    completed_routes = {
        route["route"]
        for route in result.report["routes"]
        if route["processed_frames"] > 0
    }
    assert {"cam_front.avi", "cam_rear.avi", "cam_side.avi", "cam_short.avi"} <= completed_routes
    assert any(track["route"] == "cam_front.avi" for track in result.report["tracks"])


def test_cli_invalid_config_exit_code_and_atomic_report(tmp_path: Path) -> None:
    output = tmp_path / "invalid-report.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "video_pipeline",
            "--input",
            str(PROJECT_ROOT / "samples"),
            "--model",
            str(PROJECT_ROOT / "configs" / "invalid_model_missing_windows.json"),
            "--matching",
            str(FULL_MATCHING_CONFIG),
            "--output",
            str(output),
            "--state-dir",
            str(tmp_path / "state"),
            "--reset",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["failures"][0]["code"] == "E_CONFIG_INVALID"
    assert list(tmp_path.glob("*.tmp")) == []


def test_sigterm_finishes_with_exit_zero_and_report(corpus_dir: Path, tmp_path: Path) -> None:
    output = tmp_path / "signal-report.json"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "video_pipeline",
            "--input",
            str(corpus_dir),
            "--model",
            str(MODEL_CONFIG),
            "--matching",
            str(FULL_MATCHING_CONFIG),
            "--output",
            str(output),
            "--state-dir",
            str(tmp_path / "signal-state"),
            "--reset",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Imports/config parsing happen first; the full corpus takes several seconds.
        threading.Event().wait(0.55)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, (stdout, stderr)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "graceful_stop"
    assert report["exit_reason"] == "graceful_signal"
    assert report["budget"]["frames_processed"] < 62
    assert not list((tmp_path / "signal-state").glob("*.tmp"))


def _assert_ground_truth_coverage(report: dict, truth_path: Path) -> None:
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    for route_name, route_truth in truth["routes"].items():
        by_object: dict[str, list[dict]] = {}
        for frame in route_truth["frames"]:
            by_object.setdefault(frame["object_id"], []).append(frame)
        for object_id, frames in by_object.items():
            class_name = frames[0]["class_name"]
            candidates = [
                track
                for track in report["tracks"]
                if track["route"] == route_name and track["class_name"] == class_name
            ]
            assert candidates, f"no {class_name} track for {route_name}/{object_id}"
            best_coverage = 0.0
            for track in candidates:
                track_frames = {frame["frame_index"]: frame for frame in track["frames"]}
                covered = 0
                for truth_frame in frames:
                    detected = track_frames.get(truth_frame["frame_index"])
                    if detected and iou(detected["bbox"], truth_frame["bbox"]) >= 0.25:
                        covered += 1
                best_coverage = max(best_coverage, covered / len(frames))
            assert best_coverage >= 0.7, (
                f"coverage too low for {route_name}/{object_id}: {best_coverage:.3f}"
            )
