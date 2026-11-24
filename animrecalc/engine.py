from __future__ import annotations

import copy
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from .chunks import ChunkLayout
from .errors import InterruptedAtChunk
from .evaluator import build_state_trace, evaluate_full_pose, evaluate_range
from .graph import build_chunk_dependency
from .invalidation import plan_edit
from .scheduler import BoundedChunkScheduler, SchedulerOutcome
from .storage import ChunkStore

EngineReport = dict[str, Any]


class AnimEngine:
    def __init__(
        self,
        project: dict[str, Any],
        state_dir: str | Path,
        max_workers: int | None = None,
        memory_budget_bytes: int | None = None,
        stop_event: threading.Event | None = None,
        interrupt_after_chunks: int | None = None,
        chunk_delay_ms: int = 0,
        run_id: str | None = None,
    ):
        self.project = project
        self.layout = ChunkLayout(project["spec"])
        self.bone_count = len(project["skeleton"]["bones"])
        self.max_workers = int(max_workers or project["spec"]["max_workers"])
        self.memory_budget = int(memory_budget_bytes or project["spec"]["memory_budget_bytes"])
        self.store = ChunkStore(state_dir)
        self.stop_event = stop_event or threading.Event()
        self.interrupt_after_chunks = interrupt_after_chunks
        self.chunk_delay_ms = max(0, int(chunk_delay_ms))
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.records: dict[int, dict[str, Any]] = {}
        self.session_writes = 0
        from .pipeline import project_version

        self.project_version = project_version
        self.version = project_version(project)
        self.trace, self.events = build_state_trace(project)

    def estimate_bytes(self, chunk_index: int) -> int:
        bounds = self.layout.bounds(chunk_index)
        pose_bytes = bounds.frame_count * self.bone_count * 16 * 8
        scratch_bytes = bounds.frame_count * self.bone_count * 20 * 8
        return pose_bytes + scratch_bytes

    def build_chunk_record(
        self,
        chunk_index: int,
        project: dict[str, Any],
        trace: list[dict[str, Any]],
        dirty_bone_indices: set[int] | None = None,
        reuse_record: dict[str, Any] | None = None,
        reason_summaries: list[dict[str, Any]] | None = None,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        bounds = self.layout.bounds(chunk_index)
        reuse_pose: np.ndarray | None = None
        effective_dirty = dirty_bone_indices
        if dirty_bone_indices is not None and reuse_record is not None:
            raw_pose = np.asarray(reuse_record.get("pose"), dtype=np.float64)
            expected_shape = (bounds.frame_count, self.bone_count, 16)
            if raw_pose.shape == expected_shape:
                reuse_pose = raw_pose
            else:
                effective_dirty = None
        else:
            effective_dirty = None if dirty_bone_indices is None else dirty_bone_indices

        pose = evaluate_range(
            project,
            trace,
            bounds.start_frame,
            bounds.end_frame,
            reuse_pose=reuse_pose,
            dirty_bone_indices=effective_dirty,
        )
        dependency = build_chunk_dependency(project, trace, bounds.start_frame, bounds.end_frame)
        return {
            "record_type": "anim_recalc_chunk",
            "format_version": 1,
            "version_id": version_id or self.version,
            "chunk_index": chunk_index,
            "bounds": bounds.to_dict(),
            "frame_start": bounds.start_frame,
            "frame_end": bounds.end_frame,
            "frame_count": bounds.frame_count,
            "bone_count": self.bone_count,
            "pose": pose.tolist(),
            "events": dependency["events"],
            "dependency": dependency,
            "invalidation_reasons": reason_summaries or [],
            "bone_recompute_mode": "all" if effective_dirty is None else "affected_subset",
            "dirty_bone_indices": None if effective_dirty is None else sorted(effective_dirty),
        }

    def _on_chunk_persisted(self, chunk_index: int, record: dict[str, Any]) -> None:
        self.store.write_chunk(record)
        self.records[chunk_index] = record
        self.session_writes += 1
        if self.interrupt_after_chunks is not None and self.session_writes >= int(self.interrupt_after_chunks):
            self.stop_event.set()

    def execute_indices(
        self,
        indices: set[int] | list[int],
        project: dict[str, Any] | None = None,
        trace: list[dict[str, Any]] | None = None,
        version_id: str | None = None,
        bone_masks: dict[int, set[int] | None] | None = None,
        reason_map: dict[int, list[dict[str, Any]]] | None = None,
        old_records: dict[int, dict[str, Any]] | None = None,
    ) -> SchedulerOutcome:
        project = project or self.project
        trace = trace or self.trace
        version_id = version_id or self.version
        bone_masks = bone_masks or {}
        reason_map = reason_map or {}
        old_records = old_records or self.records
        ordered = sorted(indices)

        def task(chunk_index: int, stop_event: threading.Event) -> dict[str, Any]:
            if stop_event.is_set():
                raise InterruptedAtChunk()
            if self.chunk_delay_ms:
                time.sleep(self.chunk_delay_ms / 1000.0)
            return self.build_chunk_record(
                chunk_index,
                project,
                trace,
                dirty_bone_indices=bone_masks.get(chunk_index),
                reuse_record=old_records.get(chunk_index),
                reason_summaries=reason_map.get(chunk_index, []),
                version_id=version_id,
            )

        scheduler = BoundedChunkScheduler(
            max_workers=self.max_workers,
            memory_budget_bytes=self.memory_budget,
            estimate_bytes=self.estimate_bytes,
            stop_event=self.stop_event,
        )
        outcome = scheduler.run(ordered, task, on_complete=self._on_chunk_persisted)
        if outcome.stop_requested and not outcome.first_error:
            completed = sorted(self.records.keys())
            self.store.write_interrupt_marker(
                {
                    "status": "interrupted_at_chunk_boundary",
                    "exit_code": 75,
                    "version_id": version_id,
                    "run_id": self.run_id,
                    "completed_chunk_indices": completed,
                    "completed_count": len(completed),
                    "total_chunks": self.layout.chunk_count,
                    "submitted": outcome.submitted,
                    "cancelled": outcome.cancelled,
                }
            )
            self.store.record_run(self.run_id, "interrupted")
        return outcome

    def load_matching_records(self, expected_version: str) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]], set[int]]:
        records: dict[int, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        missing: set[int] = set()
        for index in range(self.layout.chunk_count):
            bounds = self.layout.bounds(index)
            ok, reason, record = self.store.verify_chunk(
                index,
                expected_version=expected_version,
                expected_frame_count=bounds.frame_count,
            )
            if ok and record is not None:
                records[index] = record
            else:
                missing.add(index)
                failures.append({"chunk_index": index, "code": "E_DIGEST", "reason": reason})
        return records, failures, missing

    def run_or_resume_full(self) -> EngineReport:
        expected_version = self.version
        stored_version = self.store.read_version()
        version_mismatch = bool(stored_version and stored_version != expected_version)
        self.store.write_version(expected_version, {"run_id": self.run_id})

        if version_mismatch:
            self.records = {}
            dirty = set(range(self.layout.chunk_count))
            digest_failures: list[dict[str, Any]] = []
            recovery_mode = "global_version_mismatch_full_recompute"
        else:
            self.records, digest_failures, dirty = self.load_matching_records(expected_version)
            recovery_mode = "same_version_partial_recompute" if dirty else "same_version_all_present"

        missing_indices = {
            int(item["chunk_index"])
            for item in digest_failures
            if item.get("reason") == "chunk is missing"
        }
        actual_digest_failures = [
            item for item in digest_failures if item.get("reason") != "chunk is missing"
        ]
        reasons = {
            index: [
                {
                    "code": "VERSION_MISMATCH"
                    if version_mismatch
                    else ("MISSING_CHUNK" if index in missing_indices else "E_DIGEST"),
                    "reason": "global version changed; archive and recompute"
                    if version_mismatch
                    else next((item["reason"] for item in digest_failures if item["chunk_index"] == index), "missing"),
                }
            ]
            for index in dirty
        }
        outcome = self.execute_indices(
            dirty,
            bone_masks={index: None for index in dirty},
            reason_map=reasons,
            old_records={},
        )
        report = self._build_run_report(
            mode="baseline_or_recovery",
            outcome=outcome,
            recomputed=len(outcome.results),
            reused=len(self.records) - len(outcome.results),
            digest_failures=actual_digest_failures,
            missing_chunks=sorted(missing_indices),
            recovery_mode=recovery_mode,
            version_mismatch=version_mismatch,
        )
        if outcome.stop_requested:
            report["status"] = "interrupted"
            return report
        if outcome.first_error:
            report["status"] = "failed"
            self.store.atomic_write_output("report.json", report)
            self.store.record_run(self.run_id, "failed", "report.json")
            return report

        self.store.clear_interrupt_marker()
        pose, events = self.assemble_records(self.records)
        verification = self.verify_against_full(self.project, pose, events)
        report["verification"] = verification
        report["first_mismatch"] = verification["first_mismatch"]
        report["first_inconsistent_position"] = verification["first_mismatch"]
        self._write_final_outputs(pose, report, prefix="baseline")
        self.store.record_run(self.run_id, "completed", "baseline_report.json")
        return report

    def _build_run_report(self, **kwargs: Any) -> EngineReport:
        outcome: SchedulerOutcome = kwargs.pop("outcome")
        recomputed = kwargs.pop("recomputed", len(outcome.results))
        reused = kwargs.pop("reused", 0)
        report: EngineReport = {
            "ok": outcome.first_error is None and not outcome.stop_requested,
            "status": "interrupted" if outcome.stop_requested else ("failed" if outcome.first_error else "completed"),
            "run_id": self.run_id,
            "version_id": self.version,
            "total_chunks": self.layout.chunk_count,
            "frame_count": self.layout.total_frames,
            "step_ms": self.layout.step_ms,
            "recomputed_chunks": recomputed,
            "reused_chunks": reused,
            "ported_chunks": 0,
            "first_mismatch": None,
            "scheduler": {
                "max_workers": self.max_workers,
                "memory_budget_bytes": self.memory_budget,
                "submitted": outcome.submitted,
                "completed_order": outcome.completed_order,
                "cancelled": outcome.cancelled,
                "memory_wait_events": outcome.memory_wait_events,
            },
            "first_error": outcome.first_error,
            "errors": outcome.errors,
            "successful_chunks": sorted(outcome.results.keys()),
            "successful_chunk_paths": [
                str(self.store.chunk_path(index)) for index in sorted(outcome.results.keys())
            ],
            **kwargs,
        }
        return report

    def assemble_records(self, records: dict[int, dict[str, Any]]) -> tuple[np.ndarray, list[dict[str, Any]]]:
        missing = [i for i in range(self.layout.chunk_count) if i not in records]
        if missing:
            raise InterruptedAtChunk(f"cannot assemble; missing chunks {missing}")
        poses = []
        events: list[dict[str, Any]] = []
        for index in range(self.layout.chunk_count):
            record = records[index]
            poses.append(np.asarray(record["pose"], dtype=np.float64))
            events.extend(record.get("events", []))
        events.sort(key=lambda event: (event["frame"], event["transition_id"]))
        return np.concatenate(poses, axis=0), events

    def compare_pose_and_events(
        self,
        actual_pose: np.ndarray,
        actual_events: list[dict[str, Any]],
        project: dict[str, Any],
    ) -> dict[str, Any]:
        expected_pose, expected_events = evaluate_full_pose(project)
        tolerance = float(project["spec"]["pose_tolerance"])
        if actual_pose.shape != expected_pose.shape:
            return {
                "consistent": False,
                "pose_consistent": False,
                "events_consistent": False,
                "shape_mismatch": {
                    "actual": list(actual_pose.shape),
                    "expected": list(expected_pose.shape),
                },
                "first_mismatch": {"type": "shape"},
            }
        difference = np.abs(actual_pose - expected_pose)
        max_abs_diff = float(difference.max()) if difference.size else 0.0
        first_mismatch = None
        above_tolerance = np.argwhere(difference > tolerance)
        if above_tolerance.size:
            frame, bone, component = (int(x) for x in above_tolerance[0])
            bone_name = project["skeleton"]["bones"][bone]["name"]
            first_mismatch = {
                "frame": frame,
                "time_ms": frame * project["spec"]["step_ms"],
                "bone_index": bone,
                "bone": bone_name,
                "component_index": component,
                "matrix_row": component // 4,
                "matrix_column": component % 4,
                "actual": float(actual_pose[frame, bone, component]),
                "expected": float(expected_pose[frame, bone, component]),
                "diff": float(difference[frame, bone, component]),
                "tolerance": tolerance,
            }
        events_consistent = actual_events == expected_events
        first_event_mismatch = None
        if not events_consistent:
            for index, (actual, expected) in enumerate(zip(actual_events, expected_events)):
                if actual != expected:
                    first_event_mismatch = {"event_index": index, "actual": actual, "expected": expected}
                    break
            if first_event_mismatch is None:
                first_event_mismatch = {
                    "type": "event_length",
                    "actual_count": len(actual_events),
                    "expected_count": len(expected_events),
                }
        pose_consistent = first_mismatch is None
        return {
            "consistent": pose_consistent and events_consistent,
            "pose_consistent": pose_consistent,
            "events_consistent": events_consistent,
            "max_abs_diff": max_abs_diff,
            "tolerance": tolerance,
            "first_mismatch": first_mismatch or first_event_mismatch,
            "event_count": len(expected_events),
        }

    def verify_against_full(
        self,
        project: dict[str, Any],
        pose: np.ndarray,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.compare_pose_and_events(pose, events, project)

    def _port_record_to_version(self, record: dict[str, Any], version_id: str) -> dict[str, Any]:
        if record.get("version_id") == version_id:
            return record
        ported = copy.deepcopy(record)
        ported["ported_from_version"] = record.get("version_id")
        ported["version_id"] = version_id
        ported["reuse"] = "persisted_snapshot"
        self.store.write_chunk(ported)
        return ported

    def run_edit_script(self, edits: list[dict[str, Any]], verify: bool = True) -> EngineReport:
        if not self.records:
            baseline = self.run_or_resume_full()
            if baseline["status"] != "completed":
                return baseline

        step_reports: list[dict[str, Any]] = []
        total_recomputed = 0
        total_reused = 0
        total_ported = 0
        all_digest_failures: list[dict[str, Any]] = []

        for edit_index, op in enumerate(edits):
            old_project = copy.deepcopy(self.project)
            old_version = self.version
            old_trace = self.trace
            old_dependencies = []
            for chunk_index in sorted(self.records):
                dependency = dict(self.records[chunk_index]["dependency"])
                dependency["chunk_index"] = chunk_index
                old_dependencies.append(dependency)

            new_project = copy.deepcopy(old_project)
            from .edits import apply_edit

            apply_edit(new_project, op)
            from .pipeline import project_version

            new_version = project_version(new_project)
            new_trace, _ = build_state_trace(new_project)
            plan = plan_edit(
                op,
                old_project,
                new_project,
                old_trace,
                new_trace,
                old_dependencies,
                self.layout,
            )
            planned_dirty: set[int] = {int(i) for i in plan["dirty_chunks"] if 0 <= int(i) < self.layout.chunk_count}

            digest_failures: list[dict[str, Any]] = []
            dirty = set(planned_dirty)
            bone_masks: dict[int, set[int] | None] = {}
            reason_map: dict[int, list[dict[str, Any]]] = {}
            old_records_for_reuse: dict[int, dict[str, Any]] = {}

            for index in range(self.layout.chunk_count):
                bounds = self.layout.bounds(index)
                existing = self.records.get(index)
                if existing is None:
                    ok, reason, record = self.store.verify_chunk(index, old_version, bounds.frame_count)
                    if ok and record is not None:
                        existing = record
                        self.records[index] = record
                else:
                    ok, reason, _ = self.store.verify_chunk(index, old_version, bounds.frame_count)
                if not ok or existing is None:
                    digest_failures.append({"chunk_index": index, "code": "E_DIGEST", "reason": reason})
                    dirty.add(index)
                    existing = None

                if index in dirty:
                    if plan.get("all_bones") or index not in planned_dirty:
                        bone_masks[index] = None
                    else:
                        bone_masks[index] = set(plan["affected_bone_indices"])
                        if existing is not None:
                            old_records_for_reuse[index] = existing
                    reason_map[index] = [
                        {
                            "code": "INVALIDATED",
                            "kind": plan["kind"],
                            "summary": plan["summary"],
                            "plan": self._json_safe_plan(plan),
                        }
                    ]
                    if any(item["chunk_index"] == index for item in digest_failures):
                        reason_map[index].append({"code": "E_DIGEST", "reason": "persisted chunk invalid; full chunk recompute"})
                        bone_masks[index] = None
                        old_records_for_reuse.pop(index, None)

            outcome = self.execute_indices(
                dirty,
                project=new_project,
                trace=new_trace,
                version_id=new_version,
                bone_masks=bone_masks,
                reason_map=reason_map,
                old_records=old_records_for_reuse,
            )
            all_digest_failures.extend(digest_failures)

            if outcome.stop_requested:
                return {
                    "ok": False,
                    "status": "interrupted",
                    "edit_index": edit_index,
                    "version_id": new_version,
                    "step_reports": step_reports,
                    "first_error": None,
                }
            if outcome.first_error:
                report = self._build_run_report(
                    mode="incremental_edit",
                    outcome=outcome,
                    recomputed=len(outcome.results),
                    reused=0,
                    edit_index=edit_index,
                    plan=self._json_safe_plan(plan),
                    digest_failures=digest_failures,
                )
                report["status"] = "failed"
                self.store.atomic_write_output("incremental_report.json", report)
                return report

            reused_indices: list[int] = []
            for index in range(self.layout.chunk_count):
                if index in outcome.results:
                    continue
                record = self.records.get(index)
                if record is None:
                    # Should not happen because digest failures were scheduled.
                    dirty.add(index)
                    continue
                ported = self._port_record_to_version(record, new_version)
                self.records[index] = ported
                reused_indices.append(index)

            self.project = new_project
            self.trace = new_trace
            self.version = new_version
            self.store.write_version(new_version, {"run_id": self.run_id, "edit_index": edit_index})

            ported_boundary = plan.get("ported_chunks", set())
            ported_count = len([i for i in ported_boundary if 0 <= i < self.layout.chunk_count])
            total_recomputed += len(outcome.results)
            total_reused += len(reused_indices)
            total_ported += ported_count

            pose, events = self.assemble_records(self.records)
            consistency = self.verify_against_full(new_project, pose, events) if verify else None
            step_report = {
                "edit_index": edit_index,
                "operation": self._op_brief(op),
                "closure": self._json_safe_plan(plan),
                "dirty_chunks": sorted(dirty),
                "recomputed_chunks": len(outcome.results),
                "reused_chunks": len(reused_indices),
                "ported_chunks": ported_count,
                "digest_failures": digest_failures,
                "scheduler": {
                    "max_workers": self.max_workers,
                    "memory_wait_events": outcome.memory_wait_events,
                    "completed_order": outcome.completed_order,
                },
                "consistency": consistency,
                "first_mismatch": None if consistency is None else consistency["first_mismatch"],
            }
            step_reports.append(step_report)
            if consistency is not None and not consistency["consistent"]:
                final_report = {
                    "ok": False,
                    "status": "mismatch",
                    "version_id": new_version,
                    "recomputed_chunks": total_recomputed,
                    "reused_chunks": total_reused,
                    "ported_chunks": total_ported,
                    "recomputed_chunk_count": total_recomputed,
                    "reused_chunk_count": total_reused,
                    "ported_chunk_count": total_ported,
                    "first_mismatch": consistency["first_mismatch"],
                    "first_inconsistent_position": consistency["first_mismatch"],
                    "step_reports": step_reports,
                }
                self.store.atomic_write_output("incremental_report.json", final_report)
                return final_report

        pose, events = self.assemble_records(self.records)
        final_verification = self.verify_against_full(self.project, pose, events) if verify else None
        report: EngineReport = {
            "ok": True if final_verification is None else final_verification["consistent"],
            "status": "completed",
            "mode": "incremental_edit_script",
            "run_id": self.run_id,
            "version_id": self.version,
            "total_chunks": self.layout.chunk_count,
            "edit_count": len(edits),
            "recomputed_chunks": total_recomputed,
            "reused_chunks": total_reused,
            "ported_chunks": total_ported,
            "recomputed_chunk_count": total_recomputed,
            "reused_chunk_count": total_reused,
            "ported_chunk_count": total_ported,
            "unique_recomputed_chunks": sorted(
                {index for step in step_reports for index in step["dirty_chunks"]}
            ),
            "digest_failures": all_digest_failures,
            "first_mismatch": None if final_verification is None else final_verification["first_mismatch"],
            "first_inconsistent_position": None if final_verification is None else final_verification["first_mismatch"],
            "verification": final_verification,
            "step_reports": step_reports,
        }
        self._write_final_outputs(pose, report, prefix="incremental")
        self.store.clear_interrupt_marker()
        self.store.record_run(self.run_id, "completed", "incremental_report.json")
        return report

    @staticmethod
    def _json_safe_plan(plan: dict[str, Any]) -> dict[str, Any]:
        safe = copy.deepcopy(plan)
        for key in ("dirty_chunks", "ported_chunks", "affected_bone_indices"):
            if key in safe and isinstance(safe[key], set):
                safe[key] = sorted(safe[key])
        return safe

    @staticmethod
    def _op_brief(op: dict[str, Any]) -> dict[str, Any]:
        keys = ("op", "clip", "clip_id", "bone", "channel", "time", "transition_id", "id", "duration_ms")
        return {key: op[key] for key in keys if key in op}

    def _write_final_outputs(self, pose: np.ndarray, report: dict[str, Any], prefix: str) -> None:
        pose_payload = {
            "version_id": self.version,
            "frame_count": int(pose.shape[0]),
            "bone_count": self.bone_count,
            "components_per_bone": 16,
            "pose_matrices": pose.tolist(),
            "events": self.events if prefix == "baseline" else report.get("verification", {}).get("event_count"),
        }
        # Events are assembled in the report verification path; include explicit file.
        if prefix == "incremental":
            _, events = self.assemble_records(self.records)
            pose_payload["events"] = events
        output_name = f"{prefix}_pose.json"
        report_name = f"{prefix}_report.json"
        self.store.atomic_write_output(output_name, pose_payload)
        self.store.atomic_write_output(report_name, report)
