from __future__ import annotations

import concurrent.futures
import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import array_sha256, canonical_json
from .config import VideoConfig
from .decoder import (
    DecodedBlock,
    DeterministicDecoder,
    WholeDecode,
    assemble_blocks,
    compare_frame_sequences,
    verify_anchor_frame,
)
from .errors import BusinessError, ErrorCode
from .imaging import load_anchor
from .pipeline import GenerationResult, generate_latents
from .planner import DecodePlan, DecodePlanner
from .snapshot import SnapshotStore


@dataclass
class PreviewJob:
    job_id: str
    config: VideoConfig
    generation: GenerationResult
    anchor: np.ndarray | None
    decoder: DeterministicDecoder
    plan: DecodePlan
    fault_anchor: bool = False
    ready_blocks: dict[int, DecodedBlock] = field(default_factory=dict)
    chunk_locks: dict[int, threading.Lock] = field(default_factory=dict)
    in_flight: set[int] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock)
    status: str = "preview_ready"
    first_block_actual_ms: float | None = None
    whole: WholeDecode | None = None
    consistency: dict[str, Any] | None = None
    alternative_consistency: dict[str, Any] | None = None
    anchor_validation: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    ready_snapshot_version: int | None = None
    decode_snapshot_version: int | None = None
    plan_snapshot_version: int | None = None
    created_sequence: int = 0

    def __post_init__(self) -> None:
        for index in range(self.plan.num_blocks):
            self.chunk_locks[index] = threading.Lock()

    @property
    def ready_frames(self) -> int:
        return sum(block.frame_count for block in self.ready_blocks.values())

    def block_for_frame(self, frame_index: int) -> DecodedBlock | None:
        if not 0 <= frame_index < self.config.frames:
            return None
        if self.whole is not None:
            block_index = frame_index // self.plan.block_size
            start = block_index * self.plan.block_size
            end = min(self.config.frames, start + self.plan.block_size)
            return DecodedBlock(
                index=block_index,
                start_frame=start,
                end_frame=end,
                frames=self.whole.frames[start:end],
                sha256="",
                timing=self.whole.timing,
            )
        block_index = frame_index // self.plan.block_size
        block = self.ready_blocks.get(block_index)
        if block is None or not block.start_frame <= frame_index < block.end_frame:
            return None
        return block


class PreviewService:
    """One active plan lease; concurrent chunk work is allowed inside that plan."""

    def __init__(self, project_root: Path, cache_dir: Path | None = None) -> None:
        self.project_root = project_root.resolve()
        self.cache_dir = (cache_dir or self.project_root / "cache").resolve()
        self.snapshot_store = SnapshotStore(self.cache_dir / "snapshots")
        self._jobs: dict[str, PreviewJob] = {}
        self._jobs_lock = threading.RLock()
        self._gate_lock = threading.Lock()
        self._plan_gate = threading.Lock()
        self._active_job_id: str | None = None
        self._sequence = 0
        self._recent_errors: deque[dict[str, Any]] = deque(maxlen=20)
        self.startup_snapshots: dict[str, Any] = {}
        self.startup_error: dict[str, Any] | None = None
        self._load_startup_snapshots()

    def _load_startup_snapshots(self) -> None:
        try:
            self.startup_snapshots = {
                category: self.snapshot_store.observe_replacement(category)
                for category in ("decode_params", "chunk_plan", "ready_blocks")
                if (self.snapshot_store.directory / f"active-{category}.json").exists()
            }
        except BusinessError as exc:
            self.startup_error = exc.to_dict()["error"]
            self._record_error(exc)

    def record_external_error(self, error: BusinessError) -> None:
        self._record_error(error)

    def _record_error(self, error: BusinessError) -> None:
        self._recent_errors.appendleft(
            {"code": error.code.value, "message": error.message, "details": error.details}
        )

    @property
    def active_job_id(self) -> str | None:
        return self._active_job_id

    def _next_job_id(self, config: VideoConfig) -> str:
        self._sequence += 1
        digest = hashlib.sha256(
            canonical_json(
                {
                    "config": config.to_dict(),
                    "sequence": self._sequence,
                }
            ).encode("utf-8")
        ).hexdigest()[:12]
        return f"job-{digest}"

    @staticmethod
    def _config_payload(payload: dict[str, Any]) -> dict[str, Any]:
        nested = payload.get("config")
        if isinstance(nested, dict):
            return dict(nested)
        allowed = set(VideoConfig.__dataclass_fields__)
        return {key: value for key, value in payload.items() if key in allowed}

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = VideoConfig.from_dict(self._config_payload(payload))
        fault_anchor = bool(payload.get("fault_anchor", False))

        with self._gate_lock:
            if self._active_job_id is not None or self._plan_gate.locked():
                raise BusinessError(
                    ErrorCode.E_STAGE_BUSY,
                    "已有分块计划处于活动状态，后到提交不等待",
                    {
                        "active_job_id": self._active_job_id,
                        "policy": "non_blocking_reject",
                    },
                )
            if not self._plan_gate.acquire(blocking=False):
                raise BusinessError(
                    ErrorCode.E_STAGE_BUSY,
                    "分块计划提交阶段忙",
                    {"policy": "non_blocking_reject"},
                )
            job_id = self._next_job_id(config)
            self._active_job_id = job_id

        try:
            anchor = None
            anchor_path = config.resolved_anchor_path(self.project_root)
            if anchor_path is not None:
                anchor = load_anchor(anchor_path, config.width, config.height)

            generation = generate_latents(config)
            decoder = DeterministicDecoder(config, generation.latents, anchor)
            decoder.ensure_context()
            plan = DecodePlanner(config, decoder).plan(config.block_size)

            decode_payload = {
                "config": config.to_dict(),
                "generation": generation.public_metadata(),
                "anchor": (
                    None
                    if anchor is None
                    else {
                        "path": str(anchor_path),
                        "sha256": array_sha256(anchor),
                        "shape": list(anchor.shape),
                        "mode": "image_to_video",
                    }
                ),
                "decode_context": decoder.context_metadata(),
            }
            decode_snapshot = self.snapshot_store.commit("decode_params", decode_payload)
            plan_snapshot = self.snapshot_store.commit("chunk_plan", plan.to_dict())
            ready_snapshot = self.snapshot_store.commit(
                "ready_blocks",
                {
                    "job_id": job_id,
                    "block_size": plan.block_size,
                    "num_blocks": plan.num_blocks,
                    "ready_blocks": [],
                    "ready_frames": 0,
                    "first_block_actual_ms": None,
                },
            )
            job = PreviewJob(
                job_id=job_id,
                config=config,
                generation=generation,
                anchor=anchor,
                decoder=decoder,
                plan=plan,
                fault_anchor=fault_anchor,
                ready_snapshot_version=ready_snapshot.version,
                decode_snapshot_version=decode_snapshot.version,
                plan_snapshot_version=plan_snapshot.version,
                created_sequence=self._sequence,
            )
            with self._jobs_lock:
                self._jobs[job_id] = job
            return job_summary(job, self.snapshot_store, anchor_path)
        except BusinessError as exc:
            self._record_error(exc)
            self._release_gate(job_id)
            raise
        except Exception:
            self._release_gate(job_id)
            raise

    def get_job(self, job_id: str) -> PreviewJob:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise BusinessError(
                ErrorCode.E_NOT_FOUND,
                "预览任务不存在",
                {"job_id": job_id},
            )
        return job

    def decode_chunk(self, job_id: str, chunk_index: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not 0 <= chunk_index < job.plan.num_blocks:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "块索引越界",
                {"chunk_index": chunk_index, "num_blocks": job.plan.num_blocks},
            )
        with job.lock:
            if job.status == "failed" and job.last_error:
                raise BusinessError(
                    ErrorCode(job.last_error["code"]),
                    job.last_error["message"],
                    job.last_error["details"],
                )
            if chunk_index in job.ready_blocks:
                return {
                    "ok": True,
                    "already_ready": True,
                    **chunk_response(job, job.ready_blocks[chunk_index], self.snapshot_store),
                }
            if chunk_index in job.in_flight:
                raise BusinessError(
                    ErrorCode.E_STAGE_BUSY,
                    "同一块正在解码",
                    {"chunk_index": chunk_index, "policy": "non_blocking_reject"},
                )
            if len(job.in_flight) >= job.config.max_concurrent_decode_tasks:
                raise BusinessError(
                    ErrorCode.E_RESOURCE_LIMIT,
                    "并发解码任务数达到上限",
                    {
                        "resource": "concurrent_decode_tasks",
                        "limit": job.config.max_concurrent_decode_tasks,
                        "active": sorted(job.in_flight),
                        "strategy": "reject",
                    },
                )
            job.in_flight.add(chunk_index)

        chunk_lock = job.chunk_locks[chunk_index]
        try:
            with chunk_lock:
                with job.lock:
                    if chunk_index in job.ready_blocks:
                        return {
                            "ok": True,
                            "already_ready": True,
                            **chunk_response(
                                job, job.ready_blocks[chunk_index], self.snapshot_store
                            ),
                        }
                start = chunk_index * job.plan.block_size
                end = min(job.config.frames, start + job.plan.block_size)
                started = time.perf_counter()
                block = job.decoder.decode_range(start, end, block_index=chunk_index)
                wall_ms = (time.perf_counter() - started) * 1000.0

                if chunk_index == 0:
                    if job.fault_anchor and job.anchor is not None:
                        block.frames[0, 0, 0, 0] = np.uint8(
                            int(block.frames[0, 0, 0, 0]) ^ 0x01
                        )
                    if job.anchor is not None:
                        try:
                            job.anchor_validation = verify_anchor_frame(
                                block.frames, job.anchor
                            )
                        except BusinessError as exc:
                            self._fail_job(job, exc)
                            raise
                    if block.timing.total_ms > job.config.latency_budget_ms:
                        error = BusinessError(
                            ErrorCode.E_LATENCY_BUDGET,
                            "首块实测解码延迟超过预算",
                            {
                                "stage": "first_chunk_decode",
                                "completed_stages": [
                                    "prompt_hash",
                                    "seed_initial_latent",
                                    "denoise",
                                    "decode_plan",
                                    "first_chunk_decode",
                                ],
                                "decoded_blocks": len(job.ready_blocks) + 1,
                                "first_block_actual_ms": round(block.timing.total_ms, 6),
                                "wall_clock_ms": round(wall_ms, 6),
                                "estimated_first_block_ms": round(
                                    job.plan.estimated_first_block_ms, 6
                                ),
                                "latency_budget_ms": job.config.latency_budget_ms,
                                "block_size": job.plan.block_size,
                                "stage_timing_ms": block.timing.to_dict()["stage_ms"],
                            },
                        )
                        self._fail_job(job, error)
                        raise error

                with job.lock:
                    job.ready_blocks[chunk_index] = block
                    if chunk_index == 0:
                        job.first_block_actual_ms = block.timing.total_ms
                    ready_payload = {
                        "job_id": job.job_id,
                        "block_size": job.plan.block_size,
                        "num_blocks": job.plan.num_blocks,
                        "ready_blocks": [
                            job.ready_blocks[index].metadata()
                            for index in sorted(job.ready_blocks)
                        ],
                        "ready_frames": job.ready_frames,
                        "first_block_actual_ms": (
                            None
                            if job.first_block_actual_ms is None
                            else round(job.first_block_actual_ms, 6)
                        ),
                    }
                    ready_snapshot = self.snapshot_store.commit(
                        "ready_blocks", ready_payload
                    )
                    job.ready_snapshot_version = ready_snapshot.version
                return chunk_response(job, block, self.snapshot_store)
        finally:
            with job.lock:
                job.in_flight.discard(chunk_index)

    def decode_chunks(self, job_id: str, chunk_indices: list[int]) -> dict[str, Any]:
        job = self.get_job(job_id)
        unique = sorted(set(int(index) for index in chunk_indices))
        if len(unique) != len(chunk_indices):
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "并发块请求包含重复索引",
                {"chunk_indices": chunk_indices},
            )
        if not unique:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "并发块请求为空",
                {"field": "chunk_indices"},
            )
        if len(unique) > job.config.max_concurrent_decode_tasks:
            raise BusinessError(
                ErrorCode.E_RESOURCE_LIMIT,
                "请求的并发解码数超过配置上限",
                {
                    "resource": "concurrent_decode_tasks",
                    "limit": job.config.max_concurrent_decode_tasks,
                    "requested": len(unique),
                    "strategy": "reject",
                },
            )
        results: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(unique)) as executor:
            futures = {
                executor.submit(self.decode_chunk, job_id, index): index
                for index in unique
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except BusinessError as exc:
                    for pending in futures:
                        pending.cancel()
                    raise exc
        results.sort(key=lambda result: result["block"]["index"])
        return {"ok": True, "blocks": results, "job": job_status(job, self.snapshot_store)}

    def complete_job(
        self,
        job_id: str,
        compare_block_size: int | None = None,
    ) -> dict[str, Any]:
        job = self.get_job(job_id)
        try:
            with job.lock:
                if job.status == "failed" and job.last_error:
                    raise BusinessError(
                        ErrorCode(job.last_error["code"]),
                        job.last_error["message"],
                        job.last_error["details"],
                    )
                missing = [
                    index
                    for index in range(job.plan.num_blocks)
                    if index not in job.ready_blocks
                ]
                if missing:
                    raise BusinessError(
                        ErrorCode.E_INVALID_STATE,
                        "仍有块未解码，不能请求完整视频",
                        {"missing_chunk_indices": missing},
                    )
                job.status = "completing"

            assembled = assemble_blocks(
                [job.ready_blocks[index] for index in sorted(job.ready_blocks)],
                job.config.frames,
            )
            whole = job.decoder.decode_all()
            consistency = compare_frame_sequences(
                assembled,
                whole.frames,
                job.plan.block_size,
                comparison_label="preview_chunks_vs_one_shot",
            )

            alternative_size = compare_block_size or job.plan.block_size
            if alternative_size == job.plan.block_size:
                alternative_size = (
                    job.plan.block_size - 1
                    if job.plan.block_size > 1
                    else min(2, job.config.frames)
                )
            alternative_size = max(1, min(int(alternative_size), job.config.frames))
            alternative_decoder = DeterministicDecoder(
                job.config, job.generation.latents, job.anchor
            )
            alternative_blocks = alternative_decoder.decode_chunks(alternative_size)
            alternative_assembled = assemble_blocks(
                alternative_blocks, job.config.frames
            )
            alternative_consistency = compare_frame_sequences(
                alternative_assembled,
                whole.frames,
                alternative_size,
                comparison_label="alternate_chunks_vs_one_shot",
            )
            alt_anchor_validation = None
            if job.anchor is not None:
                alt_anchor_validation = verify_anchor_frame(
                    alternative_assembled, job.anchor
                )

            anchor_validation = (
                None
                if job.anchor is None
                else verify_anchor_frame(whole.frames, job.anchor)
            )
            completion_payload = {
                "job_id": job.job_id,
                "config": job.config.to_dict(),
                "generation": job.generation.public_metadata(),
                "video_sha256": whole.sha256,
                "whole_decode": whole.metadata(),
                "first_block_actual_ms": round(job.first_block_actual_ms or 0.0, 6),
                "consistency": consistency,
                "alternative_consistency": alternative_consistency,
                "anchor_validation": anchor_validation,
                "alternative_anchor_validation": alt_anchor_validation,
            }
            decode_snapshot = self.snapshot_store.commit(
                "decode_params", completion_payload
            )
            ready_payload = {
                "job_id": job.job_id,
                "block_size": job.plan.block_size,
                "num_blocks": job.plan.num_blocks,
                "ready_blocks": [
                    job.ready_blocks[index].metadata()
                    for index in sorted(job.ready_blocks)
                ],
                "ready_frames": job.ready_frames,
                "first_block_actual_ms": round(job.first_block_actual_ms or 0.0, 6),
                "complete": True,
                "video_sha256": whole.sha256,
                "consistency": consistency,
            }
            ready_snapshot = self.snapshot_store.commit("ready_blocks", ready_payload)

            with job.lock:
                job.whole = whole
                job.consistency = consistency
                job.alternative_consistency = alternative_consistency
                job.anchor_validation = anchor_validation
                job.ready_snapshot_version = ready_snapshot.version
                job.decode_snapshot_version = decode_snapshot.version
                job.status = "complete"
            result = {
                "ok": True,
                "job": job_summary(job, self.snapshot_store)["job"],
                "whole": whole.metadata(),
                "consistency": consistency,
                "alternative": {
                    "block_size": alternative_size,
                    "consistency": alternative_consistency,
                    "anchor_validation": alt_anchor_validation,
                },
                "anchor_validation": anchor_validation,
                "snapshot_versions": self.snapshot_store.versions(),
                "video": {
                    "raw_url": f"/api/jobs/{job.job_id}/video.raw",
                    "manifest_url": f"/api/jobs/{job.job_id}/manifest.json",
                    "sha256": whole.sha256,
                    "bytes": int(whole.frames.nbytes),
                },
            }
            self._release_gate(job.job_id)
            return result
        except BusinessError as exc:
            self._fail_job(job, exc)
            raise

    def _fail_job(self, job: PreviewJob, error: BusinessError) -> None:
        with job.lock:
            job.status = "failed"
            job.last_error = error.to_dict()["error"]
        self._record_error(error)
        self._release_gate(job.job_id)

    def _release_gate(self, job_id: str) -> None:
        with self._gate_lock:
            if self._active_job_id != job_id:
                return
            self._active_job_id = None
            if self._plan_gate.locked():
                self._plan_gate.release()

    def release(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        was_active = self._active_job_id == job_id
        self._release_gate(job_id)
        if job.status not in {"complete", "failed"}:
            with job.lock:
                job.status = "released"
        return {"ok": True, "released": was_active, "job_id": job_id}

    def run_demo(self, payload: dict[str, Any]) -> dict[str, Any]:
        compare_block_size = payload.get("compare_block_size")
        start = self.create_job(payload)
        job_id = start["job"]["job_id"]
        chunk_responses = []
        for index in range(start["job"]["plan"]["num_blocks"]):
            chunk_responses.append(self.decode_chunk(job_id, index))
        completion = self.complete_job(job_id, compare_block_size)
        return {
            "ok": True,
            "start": start,
            "chunks": chunk_responses,
            "completion": completion,
        }

    def get_frame(self, job_id: str, frame_index: int) -> np.ndarray:
        job = self.get_job(job_id)
        block = job.block_for_frame(frame_index)
        if block is None:
            raise BusinessError(
                ErrorCode.E_INVALID_STATE,
                "请求的帧尚未就绪",
                {
                    "job_id": job_id,
                    "frame_index": frame_index,
                    "ready_frames": job.ready_frames,
                },
            )
        return block.frames[frame_index - block.start_frame]

    def get_video_bytes(self, job_id: str) -> tuple[bytes, dict[str, Any]]:
        job = self.get_job(job_id)
        with job.lock:
            if job.whole is None:
                raise BusinessError(
                    ErrorCode.E_INVALID_STATE,
                    "完整视频尚未就绪",
                    {"job_id": job_id, "status": job.status},
                )
            frames = job.whole.frames
            payload = {
                "job_id": job_id,
                "sha256": job.whole.sha256,
                "shape": list(frames.shape),
                "dtype": str(frames.dtype),
                "bytes": int(frames.nbytes),
                "layout": "frame-major, row-major, RGB uint8",
            }
            return np.ascontiguousarray(frames).tobytes(order="C"), payload

    def state(self) -> dict[str, Any]:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        active = self._active_job_id
        current = self._jobs.get(active) if active else None
        return {
            "ok": True,
            "active_job_id": active,
            "current_job": (
                None if current is None else job_status(current, self.snapshot_store)
            ),
            "snapshots": self.snapshot_store.versions(),
            "startup_snapshots": self.startup_snapshots,
            "startup_error": self.startup_error,
            "recent_errors": list(self._recent_errors),
            "jobs": [job_summary(job, self.snapshot_store)["job"] for job in jobs[-10:]],
            "limits": {
                "max_frames": "per request config",
                "max_chunk_bytes": "per request config",
                "max_concurrent_decode_tasks": "per request config",
                "active_plan_lease": 1,
            },
        }


def chunk_response(
    job: PreviewJob,
    block: DecodedBlock,
    snapshot_store: SnapshotStore,
) -> dict[str, Any]:
    return {
        "ok": True,
        "block": block.metadata(),
        "job": job_status(job, snapshot_store),
        "frames": [
            {
                "index": index,
                "url": f"/api/jobs/{job.job_id}/frames/{index}.png",
            }
            for index in range(block.start_frame, block.end_frame)
        ],
    }


def job_status(job: PreviewJob, snapshot_store: SnapshotStore) -> dict[str, Any]:
    with job.lock:
        ready_indices = sorted(job.ready_blocks)
        return {
            "job_id": job.job_id,
            "status": job.status,
            "mode": job.config.mode,
            "stage": (
                "complete"
                if job.status == "complete"
                else "failed"
                if job.status == "failed"
                else "chunk_decoding"
                if job.in_flight
                else "preview_ready"
            ),
            "ready": {
                "chunks": ready_indices,
                "chunk_count": len(ready_indices),
                "total_chunks": job.plan.num_blocks,
                "frames": job.ready_frames,
                "total_frames": job.config.frames,
                "percent": round(
                    100.0 * job.ready_frames / max(1, job.config.frames), 2
                ),
            },
            "first_block_actual_ms": (
                None
                if job.first_block_actual_ms is None
                else round(job.first_block_actual_ms, 6)
            ),
            "in_flight_chunks": sorted(job.in_flight),
            "last_error": job.last_error,
            "snapshot_versions": {
                "decode_params": job.decode_snapshot_version,
                "chunk_plan": job.plan_snapshot_version,
                "ready_blocks": job.ready_snapshot_version,
            },
        }


def job_summary(
    job: PreviewJob,
    snapshot_store: SnapshotStore,
    anchor_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "job": {
            **job_status(job, snapshot_store),
            "config": job.config.to_dict(),
            "generation": job.generation.public_metadata(),
            "plan": job.plan.to_dict(),
            "anchor": (
                None
                if job.anchor is None
                else {
                    "path": str(anchor_path) if anchor_path else job.config.anchor_path,
                    "sha256": array_sha256(job.anchor),
                    "strength": job.config.anchor_strength,
                    "validation": job.anchor_validation,
                }
            ),
            "snapshots": snapshot_store.versions(),
            "consistency": job.consistency,
            "alternative_consistency": job.alternative_consistency,
        },
    }
