"""Deterministic chunked generation, recovery and invalidation engine."""
from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .cache import FrameCache, build_frame_cache_key
from .checkpoint import CheckpointStore
from .errors import ErrorCode, ErrorJournal, DiagnosticError
from .hashing import aggregate_hashes, array_content_hash
from .invalidation import ChangePlan, validate_recompute_reuse_partition
from .invalidation import plan_change
from .models import ClipTask, validate_revision_not_rollback
from .noise import frame_noise
from .propagation import (
    OPERATOR_VERSION,
    generate_continuous,
    propagate_frame,
    task_parameter_resolver,
)
from .resources import ResourceLimits, plan_resources


class SimulatedTermination(RuntimeError):
    """Raised in tests to emulate an abrupt stop without killing the runner."""


@dataclass
class RunStats:
    computed_frames: set[int] = field(default_factory=set)
    reused_frames: set[int] = field(default_factory=set)
    noise_frames: set[int] = field(default_factory=set)
    intermediate_state_count: int = 0
    committed_versions: list[int] = field(default_factory=list)


class DiagnosticEngine:
    def __init__(
        self,
        state_dir: str | Path,
        limits: ResourceLimits | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "tasks").mkdir(parents=True, exist_ok=True)
        self.limits = limits or ResourceLimits()
        self.cache = FrameCache(self.state_dir)
        self.checkpoints = CheckpointStore(self.state_dir)
        self.journal = ErrorJournal(self.state_dir)

    def record_error(self, error: DiagnosticError) -> dict[str, Any]:
        return self.journal.record(error)

    def latest_error(self) -> dict[str, Any] | None:
        return self.journal.latest()

    def task_snapshot_path(self, clip_id: str) -> Path:
        safe = "".join(
            ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in clip_id
        )
        return self.state_dir / "tasks" / f"{safe}.json"

    def load_task_snapshot(self, clip_id: str) -> ClipTask | None:
        path = self.task_snapshot_path(clip_id)
        if not path.exists():
            return None
        import json

        with path.open("r", encoding="utf-8") as handle:
            return ClipTask.from_dict(json.load(handle))

    def save_task_snapshot(self, task: ClipTask) -> None:
        from .atomicio import atomic_write_json

        atomic_write_json(self.task_snapshot_path(task.clip_id), task.to_dict())

    def run_clip(
        self,
        task: ClipTask,
        *,
        change_plan: ChangePlan | None = None,
        old_task: ClipTask | None = None,
        terminate_after_frame: int | None = None,
        terminate_with_signal: bool = False,
    ) -> dict[str, Any]:
        resource_plan = plan_resources(task, self.limits)
        previous_task = old_task or self.load_task_snapshot(task.clip_id)
        checkpoint_before = self.checkpoints.load(task.clip_id)
        if previous_task is None and checkpoint_before is not None:
            previous_task = ClipTask.from_dict(checkpoint_before["task"])
        validate_revision_not_rollback(task, previous_task)

        effective_plan = change_plan
        if effective_plan is None and previous_task is not None:
            semantic_change = any(
                getattr(previous_task, name) != getattr(task, name)
                for name in (
                    "revision",
                    "noise_seed",
                    "propagation_steps",
                    "block_size",
                    "motion_mode",
                    "frame_shape",
                )
            )
            if semantic_change:
                effective_plan = plan_change(previous_task, task)

        if effective_plan is not None and previous_task is None:
            raise DiagnosticError(
                ErrorCode.RECOVERY_MISMATCH,
                "cannot apply invalidation plan without previous committed parameters",
                clip_id=task.clip_id,
                field="revision",
            )

        self.save_task_snapshot(task)

        if effective_plan is not None:
            assert previous_task is not None

            def parameter_resolver(frame_index: int) -> tuple[int, int, int]:
                assert effective_plan is not None
                if frame_index >= effective_plan.affected_start_frame:
                    return (
                        task.noise_seed,
                        task.propagation_steps,
                        task.revision,
                    )
                return (
                    previous_task.noise_seed,
                    previous_task.propagation_steps,
                    previous_task.revision,
                )
        else:
            parameter_resolver = task_parameter_resolver(task)

        stats = RunStats()
        initial_checkpoint_version = 0
        initial_committed_frames: set[int] = set()
        discarded: list[dict[str, Any]] = []
        discarded_state_count = 0
        recovery_report: dict[str, Any]

        checkpoint = checkpoint_before
        if checkpoint is not None:
            self._assert_checkpoint_block_size(task, checkpoint, resource_plan)
            initial_checkpoint_version = int(checkpoint["version"])
            frame_hashes = {
                int(frame): value for frame, value in checkpoint["frame_hashes"].items()
            }
            cache_keys = {
                int(frame): value for frame, value in checkpoint["cache_keys"].items()
            }
            initial_committed_frames = set(frame_hashes)
            current_version = initial_checkpoint_version
            next_frame = int(checkpoint["next_frame"])
            blocks = list(checkpoint.get("blocks", []))

            if effective_plan is not None:
                keep_frame = effective_plan.affected_start_frame
                for frame in list(frame_hashes):
                    if frame >= keep_frame:
                        del frame_hashes[frame]
                        cache_keys.pop(frame, None)
                blocks = [block for block in blocks if int(block["start_frame"]) < keep_frame]
                start_frame = keep_frame
                for frame in effective_plan.reused_frames:
                    if frame not in frame_hashes:
                        raise DiagnosticError(
                            ErrorCode.RECOVERY_MISMATCH,
                            "unaffected upstream frame is not committed",
                            clip_id=task.clip_id,
                            field="checkpoint",
                            details={"frame": frame, "checkpoint_version": current_version},
                        )
                    self._load_committed_frame(
                        task,
                        frame,
                        frame_hashes,
                        cache_keys,
                        parameter_resolver,
                        stats,
                    )
            else:
                start_frame = next_frame
                for frame in range(start_frame):
                    self._load_committed_frame(
                        task,
                        frame,
                        frame_hashes,
                        cache_keys,
                        parameter_resolver,
                        stats,
                    )
                discarded = self.cache.discard_clip_frames_from(
                    task.clip_id, start_frame
                )
                discarded_state_count = sum(
                    int(item.get("propagation_steps") or 0)
                    for item in discarded
                    if int(item["frame"]) > 0
                )

            if start_frame > 0:
                previous_frame = self._load_committed_frame(
                    task,
                    start_frame - 1,
                    frame_hashes,
                    cache_keys,
                    parameter_resolver,
                    stats,
                )
                previous_hash = frame_hashes[start_frame - 1]
            else:
                previous_frame = None
                previous_hash = None

            recovery_report = {
                "recovered": True,
                "checkpoint_version_before": initial_checkpoint_version,
                "committed_frames": sorted(initial_committed_frames),
                "loaded_committed_frames": sorted(stats.reused_frames),
                "resumed_at_frame": start_frame,
                "discarded_uncommitted_cache_entries": discarded,
                "discarded_uncommitted_frame_count": len(discarded),
                "discarded_propagation_state_count": discarded_state_count,
                "propagation_state_policy": "in-memory state is terminated and rebuilt from block start",
                "status": "resumed",
            }
        else:
            if effective_plan is not None and effective_plan.affected_start_frame != 0:
                raise DiagnosticError(
                    ErrorCode.RECOVERY_MISMATCH,
                    "cannot reuse upstream frames because no checkpoint exists",
                    clip_id=task.clip_id,
                    field="checkpoint",
                    details={
                        "affected_start_frame": effective_plan.affected_start_frame
                    },
                )
            current_version = 0
            start_frame = 0
            frame_hashes = {}
            cache_keys = {}
            blocks = []
            previous_frame = None
            previous_hash = None
            recovery_report = {
                "recovered": False,
                "checkpoint_version_before": 0,
                "committed_frames": [],
                "loaded_committed_frames": [],
                "resumed_at_frame": 0,
                "discarded_uncommitted_cache_entries": [],
                "discarded_propagation_state_count": 0,
                "propagation_state_policy": "no prior process state",
                "status": "fresh_run",
            }

        effective_block_size = resource_plan.effective_block_size
        block_index = start_frame // effective_block_size

        while start_frame < task.total_frames:
            end_frame = min(
                (block_index + 1) * effective_block_size, task.total_frames
            )
            block_hashes: dict[int, str] = {}
            block_cache_keys: dict[int, str] = {}
            block_trace_digests: list[str] = []
            block_state_count = 0
            retained_block_frames: list[np.ndarray] = []

            for frame_index in range(start_frame, end_frame):
                seed, steps, revision_epoch = parameter_resolver(frame_index)
                noise = frame_noise(
                    task.clip_id, seed, frame_index, task.frame_shape
                )
                stats.noise_frames.add(frame_index)
                noise_hash = array_content_hash(noise)

                if frame_index == 0:
                    frame = noise
                    upstream_hash = noise_hash
                else:
                    if previous_frame is None or previous_hash is None:
                        raise DiagnosticError(
                            ErrorCode.RECOVERY_MISMATCH,
                            "missing upstream frame during chunked generation",
                            clip_id=task.clip_id,
                            field="upstream_hash",
                            details={"frame": frame_index},
                        )
                    result = propagate_frame(
                        previous_frame,
                        frame_index=frame_index,
                        propagation_steps=steps,
                        motion_mode=task.motion_mode,
                        noise=noise,
                        revision_epoch=revision_epoch,
                    )
                    frame = result.frame
                    block_trace_digests.append(result.trace_digest)
                    block_state_count += result.intermediate_state_count
                    stats.intermediate_state_count += result.intermediate_state_count
                    upstream_hash = previous_hash

                content_hash = array_content_hash(frame)
                key_material = self._build_key_material(
                    task=task,
                    frame_index=frame_index,
                    effective_block_size=effective_block_size,
                    steps=steps,
                    seed=seed,
                    revision_epoch=revision_epoch,
                    upstream_hash=upstream_hash,
                    noise_hash=noise_hash,
                )
                record = self.cache.put(key_material, frame)
                if record["cache_key"] != build_frame_cache_key(key_material):
                    raise DiagnosticError(
                        ErrorCode.STATE_INCONSISTENT,
                        "cached key failed immediate verification",
                        clip_id=task.clip_id,
                        field="cache_key",
                        details={"frame": frame_index},
                    )

                frame_hashes[frame_index] = content_hash
                cache_keys[frame_index] = record["cache_key"]
                block_hashes[frame_index] = content_hash
                block_cache_keys[frame_index] = record["cache_key"]
                stats.computed_frames.add(frame_index)
                retained_block_frames.append(frame)
                previous_frame = frame
                previous_hash = content_hash

                if terminate_after_frame == frame_index:
                    if terminate_with_signal:
                        os.kill(os.getpid(), signal.SIGKILL)
                    raise SimulatedTermination(
                        f"simulated termination after frame {frame_index}"
                    )

            block_checksum = aggregate_hashes(block_hashes)
            for verification_frame in range(start_frame, end_frame):
                verified = self.cache.load_verified(
                    block_cache_keys[verification_frame]
                )
                if (
                    verified is None
                    or verified.content_hash != block_hashes[verification_frame]
                ):
                    raise DiagnosticError(
                        ErrorCode.STATE_INCONSISTENT,
                        "block frame failed pre-commit cache verification",
                        clip_id=task.clip_id,
                        field="content_sha256",
                        details={
                            "frame": verification_frame,
                            "block_index": block_index,
                        },
                    )
            block_record = {
                "block_index": block_index,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "frame_count": end_frame - start_frame,
                "block_checksum": block_checksum,
                "frame_hashes": {str(k): v for k, v in block_hashes.items()},
                "cache_keys": {str(k): v for k, v in block_cache_keys.items()},
                "propagation_intermediate_states": block_state_count,
                "trace_digest": aggregate_hashes(block_trace_digests)
                if block_trace_digests
                else array_content_hash(np.zeros((1,), dtype=np.uint8)),
            }
            blocks.append(block_record)

            complete = end_frame == task.total_frames
            if complete:
                validate_recompute_reuse_partition(
                    task.clip_id,
                    task.total_frames,
                    stats.computed_frames,
                    stats.reused_frames,
                )

            expected_version = current_version
            checkpoint_payload = {
                "schema": "clip-checkpoint/v1",
                "clip_id": task.clip_id,
                "version": expected_version + 1,
                "revision": task.revision,
                "requested_block_size": task.block_size,
                "block_size": effective_block_size,
                "total_frames": task.total_frames,
                "completed_frames": sorted(frame_hashes),
                "next_frame": end_frame,
                "next_block": block_index + 1,
                "frame_hashes": {str(k): v for k, v in sorted(frame_hashes.items())},
                "cache_keys": {str(k): v for k, v in sorted(cache_keys.items())},
                "blocks": blocks,
                "aggregate_checksum": aggregate_hashes(frame_hashes),
                "task": task.to_dict(),
                "resource_plan": resource_plan.to_dict(),
                "complete": complete,
                "last_computed_frames": sorted(stats.computed_frames),
                "last_recomputed_frames": sorted(stats.computed_frames),
                "last_reused_frames": sorted(stats.reused_frames),
            }
            committed = self.checkpoints.commit(
                task.clip_id, checkpoint_payload, expected_version
            )
            current_version = int(committed["version"])
            stats.committed_versions.append(current_version)

            retained_block_frames.clear()
            start_frame = end_frame
            block_index += 1

        final_checkpoint = self.checkpoints.load(task.clip_id)
        assert final_checkpoint is not None
        frame_checksums = [
            {
                "frame": frame_index,
                "sha256": frame_hashes[frame_index],
                "cache_key": cache_keys[frame_index],
            }
            for frame_index in range(task.total_frames)
        ]
        return {
            "clip_id": task.clip_id,
            "status": "completed",
            "complete": True,
            "checkpoint_version": current_version,
            "committed_versions": stats.committed_versions,
            "frame_checksums": frame_checksums,
            "computed_frames": sorted(stats.computed_frames),
            "recomputed_frames": sorted(stats.computed_frames),
            "reused_frames": sorted(stats.reused_frames),
            "noise_generated_frames": sorted(stats.noise_frames),
            "propagation_intermediate_state_count": stats.intermediate_state_count,
            "resource_plan": resource_plan.to_dict(),
            "degradation": {
                "degraded": resource_plan.degraded,
                "categories": resource_plan.degradations,
            },
            "recovery": recovery_report,
            "invalidation": effective_plan.to_dict() if effective_plan else None,
            "latest_error": self.latest_error(),
        }

    def baseline_array(self, task: ClipTask) -> np.ndarray:
        return generate_continuous(task)

    def load_complete_frame_array(self, task: ClipTask) -> np.ndarray:
        checkpoint = self.checkpoints.load(task.clip_id)
        if checkpoint is None or not checkpoint.get("complete"):
            raise DiagnosticError(
                ErrorCode.RECOVERY_MISMATCH,
                "complete checkpoint is required to load final frame array",
                clip_id=task.clip_id,
                field="checkpoint",
            )
        arrays: list[np.ndarray] = []
        for frame_index in range(task.total_frames):
            cache_key = checkpoint["cache_keys"].get(str(frame_index))
            if cache_key is None:
                raise DiagnosticError(
                    ErrorCode.RECOVERY_MISMATCH,
                    "checkpoint lacks cache key for frame",
                    clip_id=task.clip_id,
                    field="cache_keys",
                    details={"frame": frame_index},
                )
            cached = self.cache.load_verified(cache_key)
            if cached is None:
                raise DiagnosticError(
                    ErrorCode.RECOVERY_MISMATCH,
                    "committed frame cache snapshot is missing",
                    clip_id=task.clip_id,
                    field="cache_key",
                    details={"frame": frame_index},
                )
            arrays.append(cached.frame)
        return np.stack(arrays, axis=0)

    def self_check(self, clip_id: str | None = None) -> dict[str, Any]:
        try:
            for record in self.cache.iter_records():
                self.cache.verify_record(record)

            if clip_id is not None:
                paths = [self.checkpoints.path_for(clip_id)]
            else:
                paths = sorted(self.checkpoints.dir.glob("*.json"))
            clip_reports: list[dict[str, Any]] = []
            for path in paths:
                cid = clip_id or path.stem
                checkpoint = self.checkpoints.load(cid)
                if checkpoint is None:
                    continue
                clip_reports.append(self._self_check_checkpoint(checkpoint))
            return {
                "ok": True,
                "checked_cache_records": sum(1 for _ in self.cache.iter_records()),
                "clips": clip_reports,
            }
        except DiagnosticError as error:
            error.details = dict(error.details or {})
            checkpoint_version = error.details.get("checkpoint_version")
            if checkpoint_version is None:
                error.details["checkpoint_version"] = None
            first_frame = error.details.get("first_deviation_frame")
            if first_frame is None:
                first_frame = error.details.get("first_frame")
            error.details["first_deviation_frame"] = first_frame
            self.record_error(error)
            raise

    def _self_check_checkpoint(self, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        clip_id = str(checkpoint["clip_id"])
        version = int(checkpoint["version"])
        total = int(checkpoint["total_frames"])
        next_frame = int(checkpoint["next_frame"])
        completed = {int(frame) for frame in checkpoint.get("completed_frames", [])}
        hashes = {int(k): v for k, v in checkpoint["frame_hashes"].items()}
        keys = {int(k): v for k, v in checkpoint["cache_keys"].items()}
        expected_completed = set(range(next_frame))

        def inconsistent(message: str, frame: int | None, **details: Any) -> DiagnosticError:
            merged = {
                "first_deviation_frame": frame,
                "first_frame": frame,
                "checkpoint_version": version,
            }
            merged.update(details)
            return DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                message,
                clip_id=clip_id,
                field=details.pop("field", "checkpoint"),
                details=merged,
            )

        if completed != expected_completed or set(hashes) != expected_completed or set(keys) != expected_completed:
            all_deviation = (
                expected_completed.symmetric_difference(completed)
                | expected_completed.symmetric_difference(set(hashes))
                | expected_completed.symmetric_difference(set(keys))
            )
            first_frame = min(all_deviation) if all_deviation else None
            raise inconsistent(
                "checkpoint completed frame set is not a consistent prefix",
                first_frame,
                field="completed_frames",
                completed=sorted(completed),
                next_frame=next_frame,
            )

        for frame in range(next_frame):
            cache_key = keys[frame]
            try:
                cached = self.cache.load_verified(cache_key)
            except DiagnosticError as error:
                details = dict(error.details or {})
                raise inconsistent(
                    "cache verification failed for checkpoint frame",
                    frame,
                    field=error.field or "cache_key",
                    source=error.to_dict(),
                ) from error
            if cached is None:
                raise inconsistent(
                    "checkpoint frame has no cache snapshot",
                    frame,
                    field="cache_key",
                    cache_key=cache_key,
                )
            if cached.content_hash != hashes[frame]:
                raise inconsistent(
                    "checkpoint hash does not match cached frame content",
                    frame,
                    field="frame_hashes",
                    checkpoint_hash=hashes[frame],
                    cached_hash=cached.content_hash,
                )
            material = cached.record["key_material"]
            upstream = material.get("upstream_hash")
            expected_upstream = hashes[frame - 1] if frame > 0 else hashes[0]
            if upstream != expected_upstream:
                raise inconsistent(
                    "cache key upstream hash does not match checkpoint dependency",
                    frame,
                    field="upstream_hash",
                    stored_upstream=upstream,
                    expected_upstream=expected_upstream,
                )

        if checkpoint.get("complete"):
            recomputed = checkpoint.get("last_recomputed_frames", [])
            reused = checkpoint.get("last_reused_frames", [])
            try:
                validate_recompute_reuse_partition(
                    clip_id, total, recomputed, reused
                )
            except DiagnosticError as error:
                raise inconsistent(
                    "recomputed/reused frame partition is inconsistent",
                    None,
                    field="frame_sets",
                    source=error.to_dict(),
                ) from error

        return {
            "clip_id": clip_id,
            "checkpoint_version": version,
            "checked_frames": next_frame,
            "complete": bool(checkpoint.get("complete")),
        }

    def _assert_checkpoint_block_size(
        self,
        task: ClipTask,
        checkpoint: Mapping[str, Any],
        resource_plan: Any,
    ) -> None:
        saved_size = checkpoint.get("block_size")
        if saved_size != resource_plan.effective_block_size:
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "configured resource plan produces a different block size than checkpoint",
                clip_id=task.clip_id,
                field="block_size",
                details={
                    "checkpoint_block_size": saved_size,
                    "planned_block_size": resource_plan.effective_block_size,
                    "checkpoint_version": checkpoint.get("version"),
                    "first_deviation_frame": checkpoint.get("next_frame"),
                },
            )

    def _load_committed_frame(
        self,
        task: ClipTask,
        frame_index: int,
        frame_hashes: Mapping[int, str],
        cache_keys: Mapping[int, str],
        parameter_resolver: Any,
        stats: RunStats,
    ) -> np.ndarray:
        cache_key = cache_keys.get(frame_index)
        if cache_key is None:
            raise DiagnosticError(
                ErrorCode.RECOVERY_MISMATCH,
                "committed frame lacks cache key",
                clip_id=task.clip_id,
                field="cache_keys",
                details={"frame": frame_index},
            )
        try:
            cached = self.cache.load_verified(cache_key)
        except DiagnosticError as error:
            raise DiagnosticError(
                ErrorCode.RECOVERY_MISMATCH,
                "committed frame cache is inconsistent and cannot be recovered",
                clip_id=task.clip_id,
                field=error.field or "cache_key",
                details={"frame": frame_index, "state_error": error.to_dict()},
            ) from error
        if cached is None or cached.content_hash != frame_hashes.get(frame_index):
            raise DiagnosticError(
                ErrorCode.RECOVERY_MISMATCH,
                "committed frame could not be recovered from cache",
                clip_id=task.clip_id,
                field="content_sha256",
                details={
                    "frame": frame_index,
                    "checkpoint_hash": frame_hashes.get(frame_index),
                    "cached_hash": getattr(cached, "content_hash", None),
                },
            )

        material = dict(cached.record["key_material"])
        seed, steps, revision_epoch = parameter_resolver(frame_index)
        expected_upstream = (
            frame_hashes[frame_index - 1]
            if frame_index > 0
            else frame_hashes[0]
        )
        semantic_checks = {
            "clip_id": task.clip_id,
            "noise_seed": seed,
            "propagation_steps": steps,
            "revision_epoch": revision_epoch,
            "frame_index": frame_index,
            "frame_range": [frame_index, frame_index + 1],
            "upstream_hash": expected_upstream,
            "operator_version": OPERATOR_VERSION,
        }
        for field_name, expected_value in semantic_checks.items():
            if material.get(field_name) != expected_value:
                raise DiagnosticError(
                    ErrorCode.RECOVERY_MISMATCH,
                    "committed cache key dependencies do not match recovery parameters",
                    clip_id=task.clip_id,
                    field=field_name,
                    details={
                        "frame": frame_index,
                        "expected": expected_value,
                        "actual": material.get(field_name),
                    },
                )
        expected_key = build_frame_cache_key(material)
        if expected_key != cache_key:
            raise DiagnosticError(
                ErrorCode.RECOVERY_MISMATCH,
                "committed cache key cannot be reconstructed",
                clip_id=task.clip_id,
                field="cache_key",
                details={"frame": frame_index},
            )
        stats.reused_frames.add(frame_index)
        return cached.frame

    def _build_key_material(
        self,
        *,
        task: ClipTask,
        frame_index: int,
        effective_block_size: int,
        steps: int,
        seed: int,
        revision_epoch: int,
        upstream_hash: str | None,
        noise_hash: str,
    ) -> dict[str, Any]:
        block_start = (
            frame_index // effective_block_size
        ) * effective_block_size
        block_end = min(task.total_frames, block_start + effective_block_size)
        return {
            "schema": "frame-cache-key-material/v1",
            "clip_id": task.clip_id,
            "noise_seed": seed,
            "revision_epoch": revision_epoch,
            "frame_range": [frame_index, frame_index + 1],
            "frame_index": frame_index,
            "block_range": [block_start, block_end],
            "block_size": effective_block_size,
            "requested_block_size": task.block_size,
            "propagation_steps": steps,
            "motion_mode": task.motion_mode,
            "frame_shape": list(task.frame_shape),
            "upstream_hash": upstream_hash,
            "input_noise_hash": noise_hash,
            "operator_version": OPERATOR_VERSION,
        }


def arrays_byte_equal(left: np.ndarray, right: np.ndarray) -> bool:
    return (
        left.shape == right.shape
        and left.dtype == right.dtype
        and left.tobytes(order="C") == right.tobytes(order="C")
    )
