from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import resolve_paths
from .consistency import validate_checkpoint_state_chain, validate_set_consistency
from .errors import TrackingError
from .gaps import complete_gaps
from .jsonio import (
    atomic_write_json,
    atomic_write_jsonl,
    canonical_json,
    canonical_line,
    sha256_bytes,
)
from .metrics import compute_metrics, verify_metric_recompute
from .models import Detection
from .revision import (
    affected_prediction_tracks,
    diff_revision_inputs,
    revision_changes_checksum,
)
from .storage import SequenceStore, assemble_output_rows, revision_dirname
from .tracker import TrackerStateMachine
from .validation import PreprocessedDataset, PreprocessedSequence, preprocess_dataset


@dataclass
class SequenceRunResult:
    seq_id: str
    revision: int
    status: str
    report: dict[str, Any]
    metrics: dict[str, Any]
    assignments: list[dict[str, Any]]
    frame_view: list[dict[str, Any]]
    segments: list[dict[str, Any]]
    completion_rows: list[dict[str, Any]]
    output_dir: Path


def _rows_checksum(rows: list[dict[str, Any]]) -> str:
    return sha256_bytes(
        "".join(canonical_line(row) for row in rows).encode("utf-8")
    )


class TrackingEngine:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        base_dir: str | Path,
        runtime_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        crash_before_commit_frame: int | None = None,
        crash_after_commit_frame: int | None = None,
    ) -> None:
        self.config = config
        self.base_dir = Path(base_dir).resolve()
        resolved = resolve_paths(self.base_dir, config)
        self.runtime_dir = (
            Path(runtime_dir).resolve() if runtime_dir is not None else resolved["runtime"]
        )
        self.output_dir = (
            Path(output_dir).resolve() if output_dir is not None else resolved["output"]
        )
        self.crash_before_commit_frame = crash_before_commit_frame
        self.crash_after_commit_frame = crash_after_commit_frame
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run_dataset(
        self,
        detection_rows: list[dict[str, Any]],
        truth_rows: list[dict[str, Any]],
        *,
        dataset_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        preprocessed = preprocess_dataset(detection_rows, truth_rows, self.config)
        sequence_results: list[SequenceRunResult] = []
        for seq_id in preprocessed.seq_ids:
            sequence = preprocessed.sequences[seq_id]
            result = self.run_sequence(sequence, dataset=preprocessed)
            sequence_results.append(result)

        summary = {
            "schema": "mt-run-summary-v1",
            "config": self.config,
            "runtime_dir": str(self.runtime_dir),
            "output_dir": str(self.output_dir),
            "dataset": dataset_manifest or {},
            "sequences": [
                {
                    "seq_id": item.seq_id,
                    "revision": item.revision,
                    "status": item.status,
                    "mode": item.report["mode"],
                    "frame_count": item.report["frame_count"],
                    "last_committed_frame": item.report["last_committed_frame"],
                    "checkpoint_version": item.report["checkpoint_version"],
                    "checkpoint_checksum": item.report["checkpoint_checksum"],
                    "mota": item.metrics["mota"],
                    "idf1": item.metrics["idf1"],
                    "id_switches": item.metrics["id_switches"],
                    "output_dir": str(item.output_dir),
                    "recovery": item.report["recovery"],
                    "resource_report": item.report["resource_report"],
                    "revision_report": item.report["revision_report"],
                }
                for item in sequence_results
            ],
        }
        atomic_write_json(self.output_dir / "run_summary.json", summary, pretty=True)
        return {
            "summary": summary,
            "preprocessed": preprocessed,
            "sequence_results": sequence_results,
        }

    def _sequence_rows(
        self,
        preprocessed: PreprocessedDataset,
        seq_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        detections = [
            row for row in preprocessed.detection_rows if row["seq_id"] == seq_id
        ]
        truth = [row for row in preprocessed.truth_rows if row["seq_id"] == seq_id]
        return detections, truth

    def run_sequence(
        self,
        sequence: PreprocessedSequence,
        *,
        dataset: PreprocessedDataset | None = None,
    ) -> SequenceRunResult:
        if dataset is None:
            raise ValueError("run_sequence requires the parent preprocessed dataset")
        detection_rows, truth_rows = self._sequence_rows(dataset, sequence.seq_id)
        incoming_revision = sequence.revision
        detection_checksum = _rows_checksum(detection_rows)
        truth_checksum = _rows_checksum(truth_rows)
        store = SequenceStore(self.runtime_dir, sequence.seq_id)
        meta = store.load_meta()
        current_revision = meta.get("current_revision")
        mode = "full"
        revision_report: dict[str, Any] = {
            "incoming_revision": incoming_revision,
            "previous_revision": current_revision,
            "affected_frames": [],
            "affected_truth_tracks": [],
            "affected_prediction_tracks": [],
            "changed_detection_frames": [],
            "changed_truth_frames": [],
            "reused_frames": 0,
            "recomputed_frames": 0,
            "discarded_old_suffix_frames": 0,
            "changes_checksum": None,
        }
        previous_checkpoint: dict[str, Any] | None = None
        checkpoints: list[dict[str, Any]] = []
        frame_count = max(sequence.frames) + 1

        if current_revision is None:
            store.clear_revision(incoming_revision)
            store.register_input(incoming_revision, detection_rows, truth_rows)
            mode = "full"
            start_frame = 0
        elif incoming_revision == current_revision:
            stored = store.input_meta(current_revision)
            if (
                stored is None
                or stored["detections_sha256"] != detection_checksum
                or stored["truth_sha256"] != truth_checksum
            ):
                raise TrackingError(
                    "E_REVISION_CONFLICT",
                    "same revision was reused with different input content",
                    seq_id=sequence.seq_id,
                    category="same_revision_content_change",
                    context={
                        "revision": incoming_revision,
                        "stored_detection_checksum": (
                            stored["detections_sha256"] if stored else None
                        ),
                        "incoming_detection_checksum": detection_checksum,
                    },
                )
            store.register_input(incoming_revision, detection_rows, truth_rows)
            checkpoints = store.validate_revision_chain(current_revision)
            previous_checkpoint = checkpoints[-1] if checkpoints else None
            if checkpoints and int(checkpoints[-1]["frame_index"]) == frame_count - 1:
                mode = "already_complete"
                start_frame = frame_count
            else:
                mode = "resume"
                start_frame = (
                    int(previous_checkpoint["frame_index"]) + 1
                    if previous_checkpoint
                    else 0
                )
        elif incoming_revision > current_revision:
            old_checkpoints = store.validate_revision_chain(current_revision)
            old_detections = store.load_input_detections(current_revision)
            old_truth = store.load_input_truth(current_revision)
            diff = diff_revision_inputs(
                old_detections,
                detection_rows,
                old_truth,
                truth_rows,
            )
            affected_predictions = affected_prediction_tracks(
                old_checkpoints,
                diff.affected_frames,
                diff.affected_truth_tracks,
            )
            old_last_frame = (
                int(old_checkpoints[-1]["frame_index"]) if old_checkpoints else -1
            )
            old_prefix_count = old_last_frame + 1
            if diff.earliest_frame is None:
                reuse_count = old_prefix_count
            else:
                reuse_count = min(diff.earliest_frame, old_prefix_count)
            store.clear_revision(incoming_revision)
            store.register_input(incoming_revision, detection_rows, truth_rows)
            reused = store.copy_prefix_to_revision(
                source_revision=current_revision,
                target_revision=incoming_revision,
                frame_count=reuse_count,
            )
            checkpoints = reused
            previous_checkpoint = checkpoints[-1] if checkpoints else None
            mode = "revision_suffix"
            start_frame = len(reused)
            revision_report.update(
                {
                    "affected_frames": diff.affected_frames,
                    "affected_truth_tracks": diff.affected_truth_tracks,
                    "affected_prediction_tracks": affected_predictions,
                    "changed_detection_frames": diff.changed_detection_frames,
                    "changed_truth_frames": diff.changed_truth_frames,
                    "earliest_affected_frame": diff.earliest_frame,
                    "reused_frames": len(reused),
                    "recomputed_frames": frame_count - len(reused),
                    "discarded_old_suffix_frames": max(0, old_prefix_count - len(reused)),
                    "changes_checksum": revision_changes_checksum(diff.changes),
                    "change_count": len(diff.changes),
                }
            )
        else:
            raise TrackingError(
                "E_REVISION_INVALID",
                "incoming revision is older than the stored revision",
                seq_id=sequence.seq_id,
                category="revision_regression",
                context={
                    "incoming_revision": incoming_revision,
                    "current_revision": current_revision,
                },
            )

        checkpoint_before = previous_checkpoint
        if mode != "already_complete":
            if previous_checkpoint:
                machine = TrackerStateMachine.from_snapshot(
                    previous_checkpoint["tracker"],
                    seq_id=sequence.seq_id,
                    revision=incoming_revision,
                    config=self.config,
                )
            else:
                machine = TrackerStateMachine(
                    seq_id=sequence.seq_id,
                    revision=incoming_revision,
                    config=self.config,
                )
            cumulative_degradation = set(
                previous_checkpoint.get("degraded_categories", [])
                if previous_checkpoint
                else []
            )
            frames_committed = 0
            for frame_index in range(start_frame, frame_count):
                raw_detections = sequence.frames[frame_index]
                detections, resource_metadata, frame_degradation = (
                    self._enforce_detection_limit(
                        sequence.seq_id,
                        frame_index,
                        raw_detections,
                    )
                )
                result = machine.process_frame(detections, frame_index)
                frame_degradation.update(result.degraded_categories)
                cumulative_degradation.update(frame_degradation)
                resource_metadata["degraded_categories"] = sorted(frame_degradation)
                if self.crash_before_commit_frame == frame_index:
                    print(
                        f"simulated crash before commit: seq={sequence.seq_id} "
                        f"frame={frame_index}",
                        flush=True,
                    )
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os._exit(137)
                store.commit_frame(
                    revision=incoming_revision,
                    frame_index=frame_index,
                    tracker_snapshot=machine.snapshot(),
                    assignments=result.assignments,
                    candidates=result.candidates,
                    frame_view=result.frame_view,
                    state_distribution=result.state_distribution,
                    degraded_categories=sorted(cumulative_degradation),
                    resource_metadata=resource_metadata,
                )
                frames_committed += 1
                if self.crash_after_commit_frame == frame_index:
                    print(
                        f"simulated crash after commit: seq={sequence.seq_id} "
                        f"frame={frame_index}",
                        flush=True,
                    )
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os._exit(137)
        else:
            frames_committed = 0

        checkpoints = store.validate_revision_chain(incoming_revision)
        if not checkpoints:
            raise TrackingError(
                "E_INCONSISTENT",
                "run completed without any committed checkpoint",
                seq_id=sequence.seq_id,
                category="missing_checkpoint",
            )
        if int(checkpoints[-1]["frame_index"]) != frame_count - 1:
            raise TrackingError(
                "E_INCONSISTENT",
                "run did not commit every frame",
                seq_id=sequence.seq_id,
                frame_index=int(checkpoints[-1]["frame_index"]) + 1,
                category="incomplete_commit_chain",
            )

        assignments = assemble_output_rows(checkpoints, "assignments")
        candidates = assemble_output_rows(checkpoints, "candidates")
        frame_view = assemble_output_rows(checkpoints, "frame_view")
        final_tracks = {
            int(item["track_id"]): item
            for item in checkpoints[-1]["tracker"]["tracks"]
        }
        from .models import Track

        track_objects = {
            track_id: Track.from_dict(item)
            for track_id, item in final_tracks.items()
        }
        gap_result = complete_gaps(
            seq_id=sequence.seq_id,
            revision=incoming_revision,
            assignments=assignments,
            final_tracks=track_objects,
            config=self.config,
            frame_range=(0, frame_count - 1),
        )
        for category in gap_result.degraded_categories:
            cumulative_degradation = set(checkpoints[-1]["degraded_categories"])
            cumulative_degradation.add(category)
            revision_report.setdefault("postprocess_degraded_categories", []).append(
                category
            )
        final_degradation = sorted(
            set(checkpoints[-1]["degraded_categories"])
            | set(gap_result.degraded_categories)
        )

        validate_checkpoint_state_chain(
            seq_id=sequence.seq_id,
            checkpoints=checkpoints,
        )
        validate_set_consistency(
            seq_id=sequence.seq_id,
            revision=incoming_revision,
            assignments=assignments,
            completion_rows=gap_result.rows,
            segments=gap_result.segments,
            checkpoints=checkpoints,
        )
        metrics = compute_metrics(
            seq_id=sequence.seq_id,
            revision=incoming_revision,
            assignments=assignments,
            truth_by_frame=sequence.truth_frames,
            completion_rows=gap_result.rows,
            gap_reports=gap_result.reports,
            segment_rows=gap_result.segments,
        )
        verify_metric_recompute(
            metrics,
            assignments=assignments,
            truth_by_frame=sequence.truth_frames,
            completion_rows=gap_result.rows,
            gap_reports=gap_result.reports,
            segment_rows=gap_result.segments,
        )

        checkpoint_before_frame = (
            int(checkpoint_before["frame_index"]) if checkpoint_before else None
        )
        checkpoint_before_version = (
            int(checkpoint_before["version"]) if checkpoint_before else None
        )
        checkpoint_before_checksum = (
            checkpoint_before["checksum"] if checkpoint_before else None
        )
        recovery_report = {
            "mode": mode,
            "resumed_from_checkpoint": mode in {"resume", "revision_suffix"},
            "start_frame": start_frame,
            "last_committed_frame_before_run": checkpoint_before_frame,
            "last_checkpoint_version_before_run": checkpoint_before_version,
            "last_checkpoint_checksum_before_run": checkpoint_before_checksum,
            "frames_not_present_in_store_before_run": max(
                0, frame_count - (start_frame if mode != "already_complete" else frame_count)
            ),
            "committed_frames_this_run": frames_committed,
            "integer_track_ids_restarted_from": start_frame,
            "integrity_ok": True,
        }
        resource_report = self._resource_report(checkpoints, final_degradation)
        report = {
            "schema": "mt-sequence-run-report-v1",
            "seq_id": sequence.seq_id,
            "revision": incoming_revision,
            "status": "complete",
            "mode": mode,
            "frame_count": frame_count,
            "first_frame": 0,
            "last_committed_frame": int(checkpoints[-1]["frame_index"]),
            "checkpoint_version": int(checkpoints[-1]["version"]),
            "checkpoint_checksum": checkpoints[-1]["checksum"],
            "checkpoint_count": len(checkpoints),
            "committed_frames_this_run": frames_committed,
            "recovery": recovery_report,
            "revision_report": revision_report,
            "resource_report": resource_report,
            "degraded_categories": final_degradation,
            "gap_completion_count": len(gap_result.reports),
            "gap_skipped_count": len(gap_result.skipped_gaps),
            "track_count": len(track_objects),
            "metrics": {
                "mota": metrics["mota"],
                "idf1": metrics["idf1"],
                "id_switches": metrics["id_switches"],
                "false_positives": metrics["false_positives"],
                "false_negatives": metrics["false_negatives"],
            },
        }
        output_dir = self._write_outputs(
            sequence=sequence,
            checkpoints=checkpoints,
            assignments=assignments,
            candidates=candidates,
            frame_view=frame_view,
            gap_result=gap_result,
            metrics=metrics,
            report=report,
        )
        return SequenceRunResult(
            seq_id=sequence.seq_id,
            revision=incoming_revision,
            status="complete",
            report=report,
            metrics=metrics,
            assignments=assignments,
            frame_view=frame_view,
            segments=gap_result.segments,
            completion_rows=gap_result.rows,
            output_dir=output_dir,
        )

    def _enforce_detection_limit(
        self,
        seq_id: str,
        frame_index: int,
        detections: tuple[Detection, ...],
    ) -> tuple[tuple[Detection, ...], dict[str, Any], set[str]]:
        limit = int(self.config["resources"]["max_detections_per_frame"])
        metadata = {
            "raw_detection_count": len(detections),
            "resident_detection_count": len(detections),
            "dropped_detections": 0,
        }
        degraded: set[str] = set()
        if len(detections) <= limit:
            return detections, metadata, degraded
        if self.config["resources"]["policy"] == "error":
            raise TrackingError(
                "E_RESOURCE_LIMIT",
                "resident detection box limit exceeded",
                seq_id=seq_id,
                frame_index=frame_index,
                category="detection_resident",
                context={"resident": len(detections), "max_detections": limit},
            )
        kept = sorted(
            detections,
            key=lambda item: (-item.score, item.track_id_true, item.x1, item.y1),
        )[:limit]
        kept.sort(key=lambda item: (item.track_id_true, -item.score))
        metadata["resident_detection_count"] = len(kept)
        metadata["dropped_detections"] = len(detections) - len(kept)
        degraded.add("detection_cap")
        return tuple(kept), metadata, degraded

    def _resource_report(
        self,
        checkpoints: list[dict[str, Any]],
        degraded_categories: list[str],
    ) -> dict[str, Any]:
        frames_with_drops = [
            int(checkpoint["frame_index"])
            for checkpoint in checkpoints
            if int(checkpoint.get("resource_metadata", {}).get("dropped_detections", 0)) > 0
        ]
        total_dropped = sum(
            int(checkpoint.get("resource_metadata", {}).get("dropped_detections", 0))
            for checkpoint in checkpoints
        )
        return {
            "limits": {
                "max_detections_per_frame": int(
                    self.config["resources"]["max_detections_per_frame"]
                ),
                "max_appearance_cache_entries": int(
                    self.config["resources"]["max_appearance_cache_entries"]
                ),
                "max_parallel_gap_tracks": int(
                    self.config["resources"]["max_parallel_gap_tracks"]
                ),
            },
            "policy": self.config["resources"]["policy"],
            "degraded_categories": degraded_categories,
            "frames_with_dropped_detections": frames_with_drops,
            "total_dropped_detections": total_dropped,
            "degradation_strategy": {
                "detection_cap": "keep top scores, break ties by truth id/bbox",
                "appearance_cache_eviction": "LRU eviction inside the per-frame working set",
                "gap_parallelism_capped": "process gap jobs in deterministic bounded batches",
            },
            "appearance_cache_scope": "per_frame_matching_working_set",
        }

    def _write_outputs(
        self,
        *,
        sequence: PreprocessedSequence,
        checkpoints: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        frame_view: list[dict[str, Any]],
        gap_result: Any,
        metrics: dict[str, Any],
        report: dict[str, Any],
    ) -> Path:
        output_dir = (
            self.output_dir
            / sequence.seq_id
            / revision_dirname(sequence.revision)
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_jsonl(output_dir / "assignments.jsonl", assignments)
        atomic_write_jsonl(output_dir / "match_candidates.jsonl", candidates)
        atomic_write_jsonl(output_dir / "track_states.jsonl", frame_view)
        atomic_write_jsonl(output_dir / "gap_completions.jsonl", gap_result.rows)
        atomic_write_json(output_dir / "gap_intervals.json", gap_result.reports)
        atomic_write_json(output_dir / "gap_skipped.json", gap_result.skipped_gaps)
        atomic_write_json(output_dir / "segments.json", gap_result.segments)
        atomic_write_json(output_dir / "metrics.json", metrics)
        checkpoint_index = [
            {
                "version": int(checkpoint["version"]),
                "frame_index": int(checkpoint["frame_index"]),
                "checksum": checkpoint["checksum"],
                "parent_version": checkpoint.get("parent_version"),
                "parent_checksum": checkpoint.get("parent_checksum"),
                "reused_from": checkpoint.get("reused_from"),
            }
            for checkpoint in checkpoints
        ]
        atomic_write_json(output_dir / "checkpoint_index.json", checkpoint_index)
        output_files = (
            "assignments.jsonl",
            "match_candidates.jsonl",
            "track_states.jsonl",
            "gap_completions.jsonl",
            "gap_intervals.json",
            "gap_skipped.json",
            "segments.json",
            "metrics.json",
            "checkpoint_index.json",
        )
        report = dict(report)
        report["output_checksums"] = {
            filename: sha256_bytes((output_dir / filename).read_bytes())
            for filename in output_files
        }
        atomic_write_json(output_dir / "run_report.json", report, pretty=True)
        return output_dir
