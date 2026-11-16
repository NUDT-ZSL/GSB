"""Parallel, deterministic, resumable two-stage video pipeline."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .atomic_io import atomic_write_json
from .budget import RouteBudget, RouteBudgetInput, allocate_frame_budget
from .config import (
    DetectionConfig,
    MatchingConfig,
    semantic_config_hash,
    validate_config_combination,
)
from .detection import SlidingWindowDetector
from .errors import DecodeError, SnapshotConfigMismatch, SnapshotFormatError
from .snapshots import SnapshotStore
from .tracking import CandidateTracker
from .video import FramePacket, VideoFrameReader, VideoInfo, scan_videos


@dataclass(frozen=True)
class PipelineResult:
    report: dict[str, Any]
    report_path: Path
    exit_code: int = 0

    @property
    def graceful_stop(self) -> bool:
        return self.report.get("status") == "graceful_stop"


@dataclass(frozen=True)
class _RouteSpec:
    order: int
    info: VideoInfo
    priority: int
    frame_budget: int
    budget: RouteBudget

    @property
    def route(self) -> str:
        return self.info.route


class _PeakMeter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)

    def exit(self) -> None:
        with self._lock:
            self._active -= 1


class Pipeline:
    def __init__(
        self,
        input_dir: str | Path,
        detection_config: DetectionConfig,
        matching_config: MatchingConfig,
        output_path: str | Path,
        state_dir: str | Path,
        *,
        resume: bool = True,
        stop_event: threading.Event | None = None,
        detector: SlidingWindowDetector | None = None,
        tracker_factory: Callable[[str], CandidateTracker] | None = None,
    ):
        validate_config_combination(detection_config, matching_config)
        self.input_dir = Path(input_dir)
        self.detection_config = detection_config
        self.matching_config = matching_config
        self.output_path = Path(output_path)
        self.resume = resume
        self.stop_event = stop_event or threading.Event()
        self.detector = detector or SlidingWindowDetector(detection_config)
        self.tracker_factory = tracker_factory or self._default_tracker
        self.config_hash = semantic_config_hash(detection_config, matching_config)
        self.snapshot_store = SnapshotStore(state_dir, self.config_hash)
        self._state_lock = threading.RLock()
        self._snapshot_lock = threading.Lock()
        self._failure_lock = threading.Lock()
        self._global_failures: list[dict[str, Any]] = []
        self._failure_signatures: set[tuple[Any, ...]] = set()
        self._snapshot_write_errors: set[tuple[str, str, str]] = set()
        self._peak_meter = _PeakMeter()
        self._started_at = time.perf_counter()

        self.videos = scan_videos(self.input_dir)
        route_inputs: list[RouteBudgetInput] = []
        preliminary: list[tuple[int, VideoInfo, int, int]] = []
        for order, info in enumerate(self.videos):
            rule = matching_config.rule_for(info.route)
            preliminary.append((order, info, rule.priority, rule.frame_budget))
            route_inputs.append(
                RouteBudgetInput(
                    route=info.route,
                    priority=rule.priority,
                    route_frame_budget=rule.frame_budget,
                    source_frame_count=info.frame_count if info.valid else 0,
                )
            )
        allocations = allocate_frame_budget(route_inputs, matching_config.total_frame_budget)
        self.route_specs: list[_RouteSpec] = [
            _RouteSpec(order, info, priority, frame_budget, allocations[info.route])
            for order, info, priority, frame_budget in preliminary
        ]
        self._spec_by_route = {spec.route: spec for spec in self.route_specs}

        for issue in matching_config.route_issues:
            self._add_failure(
                route=issue.route,
                frame_index=None,
                stage="config",
                code="E_CONFIG_INVALID",
                exception_type="ConfigError",
                message=issue.message,
            )

        self.detection_routes: dict[str, dict[str, Any]] = {}
        self.matching_routes: dict[str, dict[str, Any]] = {}
        self.recovery = {
            "resume_enabled": resume,
            "detection_snapshot_loaded": False,
            "matching_snapshot_loaded": False,
            "detection_resume_cursors": {},
            "matching_resume_cursors": {},
        }
        self._load_and_prepare_state()

    def run(self) -> PipelineResult:
        self._run_detection_stage()
        self._run_matching_stage()
        report = self._build_report()
        atomic_write_json(self.output_path, report)
        return PipelineResult(
            report=report,
            report_path=self.output_path,
            exit_code=0,
        )

    def _default_tracker(self, route: str) -> CandidateTracker:
        del route
        return CandidateTracker(
            self.matching_config.candidate_pool_capacity,
            self.matching_config.iou_match_threshold,
        )

    def _load_and_prepare_state(self) -> None:
        detection_snapshot: dict[str, Any] | None = None
        matching_snapshot: dict[str, Any] | None = None
        if self.resume:
            mismatch = False
            format_error = False
            for stage in ("detection", "matching"):
                try:
                    payload = self.snapshot_store.load(stage)
                except SnapshotConfigMismatch:
                    mismatch = True
                    continue
                except SnapshotFormatError as exc:
                    format_error = True
                    self.snapshot_store.discard(stage)
                    self._add_failure(
                        route="*",
                        frame_index=None,
                        stage="snapshot",
                        code="E_SNAPSHOT_INVALID",
                        exception_type=type(exc).__name__,
                        message=str(exc),
                    )
                    continue
                if stage == "detection":
                    detection_snapshot = payload
                else:
                    matching_snapshot = payload
            if mismatch:
                self._add_failure(
                    route="*",
                    frame_index=None,
                    stage="snapshot",
                    code="E_CONFIG_MISMATCH",
                    exception_type="SnapshotConfigMismatch",
                    message="snapshot semantic configuration hash does not match current run; restore rejected",
                )
                self.snapshot_store.discard("detection")
                self.snapshot_store.discard("matching")
                detection_snapshot = None
                matching_snapshot = None
            elif format_error:
                # Only the unreadable stage was discarded above.
                pass
            self.recovery["detection_snapshot_loaded"] = detection_snapshot is not None
            self.recovery["matching_snapshot_loaded"] = matching_snapshot is not None

        detection_route_states = (detection_snapshot or {}).get("routes", {})
        matching_route_states = (matching_snapshot or {}).get("routes", {})
        for spec in self.route_specs:
            detection_state = self._normalized_detection_state(
                spec, detection_route_states.get(spec.route)
            )
            self.detection_routes[spec.route] = detection_state
            if detection_state["cursor"] > 0:
                self.recovery["detection_resume_cursors"][spec.route] = detection_state["cursor"]

            matching_state = self._normalized_matching_state(
                spec,
                matching_route_states.get(spec.route),
                detection_state["cursor"],
            )
            self.matching_routes[spec.route] = matching_state
            if matching_state["cursor"] > 0:
                self.recovery["matching_resume_cursors"][spec.route] = matching_state["cursor"]

        if not self.route_specs:
            self._add_failure(
                route="*",
                frame_index=None,
                stage="scan",
                code="E_NO_VIDEOS",
                exception_type="InputError",
                message="no supported video files were found in the input directory",
            )

    def _normalized_detection_state(
        self, spec: _RouteSpec, raw_state: Any
    ) -> dict[str, Any]:
        fresh = {
            "cursor": 0,
            "frames": [],
            "failures": [],
            "status": "pending",
            "decode_failed_frame": None,
        }
        if not spec.info.valid:
            fresh["status"] = "decode_failed"
            fresh["decode_failed_frame"] = 0
            fresh["failures"].append(
                self._failure_document(
                    route=spec.route,
                    frame_index=0,
                    stage="decode",
                    code="E_DECODE_FAIL",
                    exception_type="DecodeError",
                    message=spec.info.error_message or "video probe failed",
                )
            )
            return fresh
        if not isinstance(raw_state, dict):
            return fresh

        cursor_raw = raw_state.get("cursor", 0)
        cursor = int(cursor_raw) if isinstance(cursor_raw, int) and not isinstance(cursor_raw, bool) else 0
        cursor = min(max(0, cursor), spec.budget.allocated_frames)
        frames_raw = raw_state.get("frames", [])
        frames_by_index: dict[int, dict[str, Any]] = {}
        if isinstance(frames_raw, list):
            for frame in frames_raw:
                if not isinstance(frame, dict):
                    continue
                frame_index = frame.get("frame_index")
                if isinstance(frame_index, int) and not isinstance(frame_index, bool):
                    if 0 <= frame_index < cursor:
                        frames_by_index[frame_index] = frame
        contiguous_frames: list[dict[str, Any]] = []
        for frame_index in range(cursor):
            frame = frames_by_index.get(frame_index)
            if frame is None:
                cursor = frame_index
                break
            contiguous_frames.append(frame)

        failures = [
            failure
            for failure in raw_state.get("failures", [])
            if isinstance(failure, dict)
        ] if isinstance(raw_state.get("failures", []), list) else []
        status = raw_state.get("status", "pending")
        if not isinstance(status, str):
            status = "pending"
        decode_failed_frame = raw_state.get("decode_failed_frame")
        if not isinstance(decode_failed_frame, int):
            decode_failed_frame = None
        state = {
            "cursor": cursor,
            "frames": contiguous_frames,
            "failures": failures,
            "status": status,
            "decode_failed_frame": decode_failed_frame,
        }
        if status == "decode_failed":
            state["frames"] = contiguous_frames
        return state

    def _normalized_matching_state(
        self,
        spec: _RouteSpec,
        raw_state: Any,
        detection_cursor: int,
    ) -> dict[str, Any]:
        fresh = {
            "cursor": 0,
            "tracks": [],
            "failures": [],
            "status": "pending",
        }
        if not isinstance(raw_state, dict):
            return fresh
        cursor_raw = raw_state.get("cursor", 0)
        cursor = int(cursor_raw) if isinstance(cursor_raw, int) and not isinstance(cursor_raw, bool) else 0
        tracks_raw = raw_state.get("tracks", [])
        tracks = [track for track in tracks_raw if isinstance(track, dict)] if isinstance(tracks_raw, list) else []
        failures = [
            failure
            for failure in raw_state.get("failures", [])
            if isinstance(failure, dict)
        ] if isinstance(raw_state.get("failures", []), list) else []
        status = raw_state.get("status", "pending")
        if not isinstance(status, str):
            status = "pending"
        if cursor > detection_cursor:
            self._add_failure(
                route=spec.route,
                frame_index=detection_cursor,
                stage="snapshot",
                code="E_SNAPSHOT_INVALID",
                exception_type="SnapshotFormatError",
                message="matching cursor is ahead of detection cursor; matching state reset",
            )
            return fresh
        # A future frame in a restored track would make the resume non-equivalent.
        for track in tracks:
            frame_list = track.get("frames", [])
            if isinstance(frame_list, list) and any(
                isinstance(frame, dict)
                and isinstance(frame.get("frame_index"), int)
                and frame["frame_index"] >= cursor
                for frame in frame_list
            ):
                self._add_failure(
                    route=spec.route,
                    frame_index=cursor,
                    stage="snapshot",
                    code="E_SNAPSHOT_INVALID",
                    exception_type="SnapshotFormatError",
                    message="matching snapshot contains track frames beyond its cursor; matching state reset",
                )
                return fresh
        return {
            "cursor": cursor,
            "tracks": tracks,
            "failures": failures,
            "status": status,
        }

    def _run_detection_stage(self) -> None:
        pending = [
            spec
            for spec in self.route_specs
            if spec.info.valid
            and self.detection_routes[spec.route]["status"] != "decode_failed"
            and self.detection_routes[spec.route]["cursor"] < spec.budget.allocated_frames
        ]
        self._run_route_workers(pending, self._run_detection_route, stage="detection")
        # Terminal routes that were already complete still need a final snapshot normalization.
        for spec in self.route_specs:
            state = self.detection_routes[spec.route]
            if state["status"] == "pending" and state["cursor"] >= spec.budget.allocated_frames:
                state["status"] = "budget_truncated" if spec.budget.truncated else "completed"
            elif state["status"] == "pending" and self.stop_event.is_set():
                state["status"] = "graceful_stop"
        self._persist_detection(force=True)

    def _run_detection_route(self, spec: _RouteSpec) -> None:
        state = self.detection_routes[spec.route]
        if state["cursor"] >= spec.budget.allocated_frames:
            state["status"] = "budget_truncated" if spec.budget.truncated else "completed"
            self._persist_detection(force=True)
            return
        stopped = False
        decode_error: DecodeError | None = None
        snapshot_counter = 0
        try:
            with VideoFrameReader(spec.info, start_frame=state["cursor"]) as reader:
                remaining = spec.budget.allocated_frames - state["cursor"]
                for packet in reader.frames(remaining):
                    if self.stop_event.is_set():
                        stopped = True
                        break
                    candidates = self._detect_packet(spec, packet)
                    with self._state_lock:
                        state["frames"].append(
                            {
                                "frame_index": packet.frame_index,
                                "timestamp_ms": packet.timestamp_ms,
                                "candidates": candidates,
                            }
                        )
                        state["cursor"] = packet.frame_index + 1
                        snapshot_counter += 1
                    if snapshot_counter % self.matching_config.snapshot_every_frames == 0:
                        self._persist_detection(force=True)
        except DecodeError as exc:
            decode_error = exc
            self._add_route_failure(
                state,
                self._failure_document(
                    route=spec.route,
                    frame_index=exc.frame_index,
                    stage="decode",
                    code="E_DECODE_FAIL",
                    exception_type=type(exc).__name__,
                    message=str(exc),
                ),
            )
        except Exception as exc:
            self._add_route_failure(
                state,
                self._failure_document(
                    route=spec.route,
                    frame_index=state["cursor"],
                    stage="decode",
                    code="E_DECODE_FAIL",
                    exception_type=type(exc).__name__,
                    message=f"reader failure: {exc}",
                ),
            )
            decode_error = DecodeError(state["cursor"], f"reader failure: {exc}")

        if decode_error is not None:
            state["status"] = "decode_failed"
            state["decode_failed_frame"] = decode_error.frame_index
        elif stopped:
            state["status"] = "graceful_stop"
        elif state["cursor"] >= spec.budget.allocated_frames:
            state["status"] = "budget_truncated" if spec.budget.truncated else "completed"
        else:
            state["status"] = "graceful_stop"
        self._persist_detection(force=True)

    def _detect_packet(self, spec: _RouteSpec, packet: FramePacket) -> list[dict[str, Any]]:
        try:
            return self.detector.detect_frame(packet.image, spec.route, packet.frame_index)
        except Exception as exc:
            state = self.detection_routes[spec.route]
            self._add_route_failure(
                state,
                self._failure_document(
                    route=spec.route,
                    frame_index=packet.frame_index,
                    stage="detection",
                    code="E_DETECTION_FAIL",
                    exception_type=type(exc).__name__,
                    message=str(exc),
                ),
            )
            return []

    def _run_matching_stage(self) -> None:
        pending = [
            spec
            for spec in self.route_specs
            if self.matching_routes[spec.route]["cursor"]
            < self.detection_routes[spec.route]["cursor"]
        ]
        self._run_route_workers(pending, self._run_matching_route, stage="matching")
        for spec in self.route_specs:
            state = self.matching_routes[spec.route]
            detection_state = self.detection_routes[spec.route]
            if state["cursor"] >= detection_state["cursor"]:
                state["status"] = detection_state["status"]
            elif self.stop_event.is_set():
                state["status"] = "graceful_stop"
        self._persist_matching(force=True)

    def _run_matching_route(self, spec: _RouteSpec) -> None:
        state = self.matching_routes[spec.route]
        detection_state = self.detection_routes[spec.route]
        detection_cursor = detection_state["cursor"]
        if state["cursor"] > detection_cursor:
            state["cursor"] = 0
            state["tracks"] = []
        tracker = self.tracker_factory(spec.route)
        if state["tracks"]:
            tracker = CandidateTracker.from_state(
                self.matching_config.candidate_pool_capacity,
                self.matching_config.iou_match_threshold,
                state["tracks"],
            )
        frame_map = {
            int(frame["frame_index"]): frame
            for frame in detection_state["frames"]
            if isinstance(frame, dict) and isinstance(frame.get("frame_index"), int)
        }
        stopped = False
        snapshot_counter = 0
        for frame_index in range(state["cursor"], detection_cursor):
            if self.stop_event.is_set():
                stopped = True
                break
            frame = frame_map.get(frame_index)
            if frame is None:
                self._add_route_failure(
                    state,
                    self._failure_document(
                        route=spec.route,
                        frame_index=frame_index,
                        stage="matching",
                        code="E_MATCHING_FAIL",
                        exception_type="MissingFrameError",
                        message="detection frame is missing from contiguous snapshot state",
                    ),
                )
                break
            tracks_before_frame = tracker.to_state()
            try:
                tracker.process_frame(frame)
            except Exception as exc:
                tracker = CandidateTracker.from_state(
                    self.matching_config.candidate_pool_capacity,
                    self.matching_config.iou_match_threshold,
                    tracks_before_frame,
                )
                self._add_route_failure(
                    state,
                    self._failure_document(
                        route=spec.route,
                        frame_index=frame_index,
                        stage="matching",
                        code="E_MATCHING_FAIL",
                        exception_type=type(exc).__name__,
                        message=str(exc),
                    ),
                )
            with self._state_lock:
                state["cursor"] = frame_index + 1
                state["tracks"] = tracker.to_state()
                snapshot_counter += 1
            if snapshot_counter % self.matching_config.snapshot_every_frames == 0:
                self._persist_matching(force=True)

        if stopped:
            state["status"] = "graceful_stop"
        elif state["cursor"] >= detection_cursor:
            state["status"] = detection_state["status"]
        else:
            state["status"] = "graceful_stop"
        self._persist_matching(force=True)

    def _run_route_workers(
        self,
        specs: list[_RouteSpec],
        worker: Callable[[_RouteSpec], None],
        *,
        stage: str,
    ) -> None:
        if not specs or self.stop_event.is_set():
            return
        workers = min(self.matching_config.max_workers, len(specs))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"vp-{stage}") as executor:
            futures = [executor.submit(self._metered_worker, worker, spec, stage) for spec in specs]
            for future in futures:
                future.result()

    def _metered_worker(
        self,
        worker: Callable[[_RouteSpec], None],
        spec: _RouteSpec,
        stage: str,
    ) -> None:
        self._peak_meter.enter()
        try:
            worker(spec)
        except Exception as exc:
            self._add_failure(
                route=spec.route,
                frame_index=self.detection_routes.get(spec.route, {}).get("cursor"),
                stage=stage,
                code="E_INTERNAL_ERROR",
                exception_type=type(exc).__name__,
                message=f"unhandled route worker failure: {exc}",
            )
        finally:
            self._peak_meter.exit()

    def _persist_detection(self, *, force: bool = False) -> None:
        del force
        with self._state_lock:
            payload = {
                "frames_processed": sum(state["cursor"] for state in self.detection_routes.values()),
                "routes": self.detection_routes,
            }
            with self._snapshot_lock:
                try:
                    self.snapshot_store.save("detection", payload)
                except Exception as exc:
                    signature = ("detection", type(exc).__name__, str(exc))
                    if signature not in self._snapshot_write_errors:
                        self._snapshot_write_errors.add(signature)
                        self._add_failure(
                            route="*",
                            frame_index=None,
                            stage="snapshot",
                            code="E_SNAPSHOT_WRITE",
                            exception_type=type(exc).__name__,
                            message=f"cannot write detection snapshot: {exc}",
                        )

    def _persist_matching(self, *, force: bool = False) -> None:
        del force
        with self._state_lock:
            payload = {
                "frames_processed": sum(state["cursor"] for state in self.matching_routes.values()),
                "routes": self.matching_routes,
            }
            with self._snapshot_lock:
                try:
                    self.snapshot_store.save("matching", payload)
                except Exception as exc:
                    signature = ("matching", type(exc).__name__, str(exc))
                    if signature not in self._snapshot_write_errors:
                        self._snapshot_write_errors.add(signature)
                        self._add_failure(
                            route="*",
                            frame_index=None,
                            stage="snapshot",
                            code="E_SNAPSHOT_WRITE",
                            exception_type=type(exc).__name__,
                            message=f"cannot write matching snapshot: {exc}",
                        )

    def _add_route_failure(self, state: dict[str, Any], failure: dict[str, Any]) -> None:
        signature = self._failure_signature(failure)
        with self._state_lock:
            with self._failure_lock:
                if signature in self._failure_signatures:
                    return
                self._failure_signatures.add(signature)
                state.setdefault("failures", []).append(failure)

    def _add_failure(
        self,
        *,
        route: str,
        frame_index: int | None,
        stage: str,
        code: str,
        exception_type: str,
        message: str,
    ) -> None:
        with self._failure_lock:
            self._add_failure_unlocked(
                route=route,
                frame_index=frame_index,
                stage=stage,
                code=code,
                exception_type=exception_type,
                message=message,
            )

    def _add_failure_unlocked(
        self,
        *,
        route: str,
        frame_index: int | None,
        stage: str,
        code: str,
        exception_type: str,
        message: str,
    ) -> None:
        failure = self._failure_document(
            route=route,
            frame_index=frame_index,
            stage=stage,
            code=code,
            exception_type=exception_type,
            message=message,
        )
        signature = self._failure_signature(failure)
        if signature in self._failure_signatures:
            return
        self._failure_signatures.add(signature)
        self._global_failures.append(failure)

    @staticmethod
    def _failure_signature(failure: dict[str, Any]) -> tuple[Any, ...]:
        return (
            failure.get("route"),
            failure.get("frame_index"),
            failure.get("stage"),
            failure.get("code"),
            failure.get("exception_type"),
            failure.get("message"),
        )

    @staticmethod
    def _failure_document(
        *,
        route: str,
        frame_index: int | None,
        stage: str,
        code: str,
        exception_type: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "route": route,
            "frame_index": frame_index,
            "stage": stage,
            "code": code,
            "exception_type": exception_type,
            "message": message,
        }

    def _build_report(self) -> dict[str, Any]:
        failures = list(self._global_failures)
        for spec in self.route_specs:
            failures.extend(self.detection_routes[spec.route].get("failures", []))
            failures.extend(self.matching_routes[spec.route].get("failures", []))
            budget = spec.budget
            if budget.truncated:
                failures.append(
                    self._failure_document(
                        route=spec.route,
                        frame_index=budget.truncated_frame,
                        stage="budget",
                        code="E_BUDGET_EXHAUSTED",
                        exception_type="BudgetExhausted",
                        message=(
                            f"route has {budget.source_frame_count} source frames and a route/global "
                            f"allocation of {budget.allocated_frames}; next unprocessed frame is "
                            f"{budget.truncated_frame}"
                        ),
                    )
                )
        failures = self._deduplicate_failures(failures)
        failures.sort(
            key=lambda failure: (
                str(failure.get("route", "")),
                -1 if failure.get("frame_index") is None else int(failure["frame_index"]),
                str(failure.get("stage", "")),
                str(failure.get("code", "")),
                str(failure.get("exception_type", "")),
                str(failure.get("message", "")),
            )
        )

        tracks = self._build_tracks()
        routes = self._build_route_reports(failures)
        allocated_frames = sum(spec.budget.allocated_frames for spec in self.route_specs)
        processed_frames = sum(state["cursor"] for state in self.detection_routes.values())
        elapsed = round(time.perf_counter() - self._started_at, 6)
        budget_report = {
            "max_workers": self.matching_config.max_workers,
            "total_frame_budget": self.matching_config.total_frame_budget,
            "allocated_frames": allocated_frames,
            "frames_processed": processed_frames,
            "remaining_frame_budget": max(
                0, self.matching_config.total_frame_budget - processed_frames
            ),
            "peak_workers": self._peak_meter.peak,
            "elapsed_seconds": elapsed,
        }
        status = "graceful_stop" if self.stop_event.is_set() else "completed"
        semantic_result = {
            "tracks": tracks,
            "routes": routes,
            "failures": failures,
            "budget": {
                "total_frame_budget": budget_report["total_frame_budget"],
                "allocated_frames": budget_report["allocated_frames"],
                "frames_processed": budget_report["frames_processed"],
            },
        }
        result_hash = _stable_hash(semantic_result)
        return {
            "schema_version": 1,
            "status": status,
            "config_hash": self.config_hash,
            "result_hash": result_hash,
            "exit_reason": "graceful_signal" if self.stop_event.is_set() else "completed",
            "tracks": tracks,
            "routes": routes,
            "failures": failures,
            "budget": budget_report,
            "recovery": self.recovery,
        }

    def _build_tracks(self) -> list[dict[str, Any]]:
        records: list[tuple[int, int, int, str, dict[str, Any]]] = []
        for spec in self.route_specs:
            for track in self.matching_routes[spec.route].get("tracks", []):
                if not isinstance(track, dict):
                    continue
                try:
                    local_id = int(track["local_id"])
                    created_frame = int(track["created_frame"])
                except (KeyError, TypeError, ValueError):
                    continue
                records.append((created_frame, spec.order, local_id, spec.route, track))
        records.sort(key=lambda item: (item[0], item[1], item[2]))

        output: list[dict[str, Any]] = []
        next_track_id = 1
        for _, _, _, route, track in records:
            frame_records = sorted(
                (
                    {
                        "frame_index": int(frame["frame_index"]),
                        "timestamp_ms": round(float(frame["timestamp_ms"]), 6),
                        "bbox": [int(value) for value in frame["bbox"]],
                        "score": round(float(frame["score"]), 12),
                        "stable_key": str(frame.get("stable_key", "")),
                    }
                    for frame in track.get("frames", [])
                    if isinstance(frame, dict)
                ),
                key=lambda frame: frame["frame_index"],
            )
            if not frame_records:
                continue
            output.append(
                {
                    "track_id": next_track_id,
                    "class_name": str(track["class_name"]),
                    "route": route,
                    "created_frame": int(track["created_frame"]),
                    "first_frame": frame_records[0]["frame_index"],
                    "last_frame": frame_records[-1]["frame_index"],
                    "center": [round(float(value), 6) for value in track.get("center", [0.0, 0.0])],
                    "velocity": [round(float(value), 6) for value in track.get("velocity", [0.0, 0.0])],
                    "frames": frame_records,
                }
            )
            next_track_id += 1
        return output

    def _build_route_reports(
        self, failures: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for spec in self.route_specs:
            detection_state = self.detection_routes[spec.route]
            matching_state = self.matching_routes[spec.route]
            route_failures = [failure for failure in failures if failure.get("route") == spec.route]
            if detection_state["status"] == "decode_failed":
                status = "decode_failed"
            elif (
                detection_state["status"] == "graceful_stop"
                or matching_state["status"] == "graceful_stop"
                or (self.stop_event.is_set() and matching_state["status"] == "pending")
            ):
                status = "graceful_stop"
            elif spec.budget.truncated:
                status = "budget_truncated"
            elif route_failures:
                status = "completed_with_errors"
            else:
                status = "completed"
            reports.append(
                {
                    "route": spec.route,
                    "priority": spec.priority,
                    "frame_budget": spec.frame_budget,
                    "source_frame_count": spec.info.frame_count,
                    "allocated_frames": spec.budget.allocated_frames,
                    "processed_frames": detection_state["cursor"],
                    "matched_frames": matching_state["cursor"],
                    "truncated_frame": spec.budget.truncated_frame,
                    "status": status,
                    "decode_failed_frame": detection_state.get("decode_failed_frame"),
                    "video": {
                        "width": spec.info.width,
                        "height": spec.info.height,
                        "fps": spec.info.fps,
                        "valid": spec.info.valid,
                    },
                }
            )
        return reports

    @staticmethod
    def _deduplicate_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set()
        output: list[dict[str, Any]] = []
        for failure in failures:
            signature = Pipeline._failure_signature(failure)
            if signature in seen:
                continue
            seen.add(signature)
            output.append(failure)
        return output


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
