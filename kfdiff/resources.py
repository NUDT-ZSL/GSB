"""Concurrency and resource caps.

* Ready-frame queries are unlimited concurrent readers and track observed
  maximum concurrency for the self-check / dashboard.
* Plan submission and snapshot commitment share one non-blocking stage lock;
  a conflicting caller immediately receives ``E_STAGE_BUSY``.
* Parallel block jobs use bounded slots; exhaustion either raises
  ``E_RESOURCE_LIMIT`` or degrades to serial execution with a recorded category.
* Per-clip resident pixel memory has a configurable hard cap.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from .errors import E_RESOURCE_LIMIT, E_STAGE_BUSY, KFError
import os

try:  # Linux / WSL advisory lock for cross-process stage mutual exclusion
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


class ResourceGovernor:
    def __init__(self, config: Dict[str, Any], lock_path: Optional[Any] = None):
        memory_cfg = config.get("memory", {})
        self.max_clip_resident_bytes = int(memory_cfg.get("max_clip_resident_bytes", 256 * 1024 * 1024))
        self.max_parallel_jobs = max(1, int(memory_cfg.get("max_parallel_jobs", 2)))
        self.on_parallel_limit = str(memory_cfg.get("on_parallel_limit", "serialize"))

        self._stage_lock = threading.Lock()
        self._stage_info_lock = threading.Lock()
        self._stage_holder: Optional[Dict[str, Any]] = None

        self._query_cond = threading.Condition()
        self.active_queries = 0
        self.max_concurrent_queries_observed = 0
        self.total_queries = 0

        self._job_slots = threading.BoundedSemaphore(self.max_parallel_jobs)
        self._job_counter_lock = threading.Lock()
        self.active_jobs = 0
        self.max_concurrent_jobs_observed = 0

        self.degradations: List[Dict[str, Any]] = []
        self.lock_path = str(lock_path) if lock_path else None
        self._lock_fd: Optional[int] = None

    def stage_status(self) -> Optional[Dict[str, Any]]:
        with self._stage_info_lock:
            if not self._stage_holder:
                return None
            holder = dict(self._stage_holder)
            holder["held_for_ms"] = round((time.time() - holder["since"]) * 1000.0, 3)
            return holder

    @contextmanager
    def stage(self, label: str):
        """Non-blocking exclusive stage lease; raises E_STAGE_BUSY."""
        if not self._stage_lock.acquire(blocking=False):
            holder = self.stage_status() or {"label": "unknown"}
            raise KFError(
                E_STAGE_BUSY,
                "another generation plan or snapshot commit is already active",
                {"requested_label": label, "current_holder": holder, "wait_policy": "non_blocking"},
            )
        lock_fd = None
        if self.lock_path and fcntl is not None:
            try:
                lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.ftruncate(lock_fd, 0)
                os.write(lock_fd, f"{label} pid={os.getpid()}".encode("utf-8"))
                self._lock_fd = lock_fd
            except (BlockingIOError, OSError) as exc:
                if lock_fd is not None:
                    os.close(lock_fd)
                self._stage_lock.release()
                holder = self.stage_status() or {"label": "another-process"}
                raise KFError(
                    E_STAGE_BUSY,
                    "another process holds the generation/snapshot stage lock",
                    {"requested_label": label, "lock_path": self.lock_path,
                     "current_holder": holder, "os_error": str(exc),
                     "wait_policy": "non_blocking"},
                )
        with self._stage_info_lock:
            self._stage_holder = {"label": label, "since": time.time()}
        try:
            yield
        finally:
            with self._stage_info_lock:
                self._stage_holder = None
            self._stage_lock.release()
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
                self._lock_fd = None

    @contextmanager
    def query(self):
        with self._query_cond:
            self.active_queries += 1
            self.total_queries += 1
            self.max_concurrent_queries_observed = max(
                self.max_concurrent_queries_observed, self.active_queries
            )
        try:
            yield
        finally:
            with self._query_cond:
                self.active_queries -= 1
                self._query_cond.notify_all()

    def try_acquire_job_slot(self) -> bool:
        acquired = self._job_slots.acquire(blocking=False)
        if acquired:
            with self._job_counter_lock:
                self.active_jobs += 1
                self.max_concurrent_jobs_observed = max(
                    self.max_concurrent_jobs_observed, self.active_jobs
                )
        return acquired

    def occupy_all_slots_for_test(self) -> int:
        """Deterministically exhaust slots (tests / degradation demo)."""
        occupied = 0
        while self.try_acquire_job_slot():
            occupied += 1
            if occupied > self.max_parallel_jobs:
                break
        return occupied

    def release_job_slot(self) -> None:
        with self._job_counter_lock:
            self.active_jobs = max(0, self.active_jobs - 1)
        self._job_slots.release()

    def note_degradation(self, category: str, detail: Dict[str, Any]) -> Dict[str, Any]:
        record = {"category": category, "at": time.time(), **detail}
        self.degradations.append(record)
        return record

    def check_clip_memory(self, clip_id: str, estimated_bytes: int) -> None:
        if estimated_bytes > self.max_clip_resident_bytes:
            raise KFError(
                E_RESOURCE_LIMIT,
                "estimated resident pixel memory for clip exceeds configured cap",
                {
                    "clip_id": clip_id,
                    "estimated_bytes": int(estimated_bytes),
                    "limit_bytes": self.max_clip_resident_bytes,
                    "limit_name": "max_clip_resident_bytes",
                    "degradation_available": False,
                },
            )

    def dispatch_block(self, fn, *, clip_id: str, block_index: int):
        """Run a block with slot control; degrade to serial when exhausted."""
        if self.try_acquire_job_slot():
            try:
                return fn(), "parallel", None
            finally:
                self.release_job_slot()

        if self.on_parallel_limit == "reject":
            raise KFError(
                E_RESOURCE_LIMIT,
                "parallel generation task slots exhausted and policy is reject",
                {
                    "clip_id": clip_id,
                    "block_index": block_index,
                    "max_parallel_jobs": self.max_parallel_jobs,
                    "limit_name": "max_parallel_jobs",
                },
            )
        degradation = self.note_degradation(
            "parallel_to_serial",
            {
                "clip_id": clip_id,
                "block_index": block_index,
                "max_parallel_jobs": self.max_parallel_jobs,
                "reason": "all parallel job slots busy; block executed serially",
            },
        )
        return fn(), "serial_degraded", degradation

    def status(self) -> Dict[str, Any]:
        return {
            "stage_holder": self.stage_status(),
            "active_queries": self.active_queries,
            "max_concurrent_queries_observed": self.max_concurrent_queries_observed,
            "total_queries": self.total_queries,
            "active_jobs": self.active_jobs,
            "max_concurrent_jobs_observed": self.max_concurrent_jobs_observed,
            "max_parallel_jobs": self.max_parallel_jobs,
            "max_clip_resident_bytes": self.max_clip_resident_bytes,
            "on_parallel_limit": self.on_parallel_limit,
            "degradation_count": len(self.degradations),
            "degradations": self.degradations[-10:],
        }


def estimate_clip_resident_bytes(task, *, store_trajectory: bool = True) -> int:
    """Conservative resident memory estimate for one clip generation."""
    pixel_bytes = task.total_frames * task.pixels_per_frame  # interpolated u8
    keyframe_bytes = task.keyframe_count * task.pixels_per_frame
    trajectory_bytes = 0
    if store_trajectory:
        trajectory_bytes = (
            task.keyframe_count * task.diffusion_steps * task.pixels_per_frame * 4
        )
    noise_stack_bytes = task.keyframe_count * task.pixels_per_frame * 8
    return int(pixel_bytes + keyframe_bytes + trajectory_bytes + noise_stack_bytes)
