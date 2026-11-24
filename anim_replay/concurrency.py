from __future__ import annotations

import heapq
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import BudgetError
from .models import Spec
from .sampler import ClipPose, ClipPoseSet, clip_local_offsets, evaluate_clip_offsets


@dataclass
class _Waiter:
    clip_id: str
    batch: int
    sequence: int
    amount: int

    @property
    def sort_key(self) -> tuple[str, int, int]:
        return (self.clip_id, self.batch, self.sequence)


class PriorityMemoryBudget:
    """A deterministic priority lease for in-flight sampling memory.

    When the budget cannot satisfy every waiter, the lease with the smallest
    (clip id, batch index) key is granted first.  This makes wait order stable
    across runs without changing interpolation precision.
    """

    def __init__(self, limit_bytes: int) -> None:
        if limit_bytes <= 0:
            raise BudgetError("memory budget must be positive", details={"limit_bytes": limit_bytes})
        self.limit_bytes = int(limit_bytes)
        self._condition = threading.Condition()
        self._heap: list[tuple[tuple[str, int, int], _Waiter]] = []
        self._sequence = 0
        self.used_bytes = 0
        self.peak_bytes = 0
        self.waits = 0

    def acquire(self, clip_id: str, batch: int, amount: int) -> None:
        amount = int(amount)
        if amount < 0:
            raise ValueError("negative memory lease")
        if amount > self.limit_bytes:
            raise BudgetError(
                "a sampling batch exceeds the configured memory budget",
                details={
                    "clip": clip_id,
                    "batch": batch,
                    "batch_bytes": amount,
                    "limit_bytes": self.limit_bytes,
                },
            )
        with self._condition:
            waiter = _Waiter(clip_id, batch, self._sequence, amount)
            self._sequence += 1
            heapq.heappush(self._heap, (waiter.sort_key, waiter))
            while True:
                head_key, head = self._heap[0]
                if head is waiter and self.used_bytes + amount <= self.limit_bytes:
                    heapq.heappop(self._heap)
                    self.used_bytes += amount
                    self.peak_bytes = max(self.peak_bytes, self.used_bytes)
                    return
                self.waits += 1
                self._condition.wait()

    def release(self, amount: int) -> None:
        with self._condition:
            self.used_bytes -= int(amount)
            if self.used_bytes < 0:
                self.used_bytes = 0
            self._condition.notify_all()


def _pose_bytes(pose: ClipPose, bone_count: int) -> int:
    # Python object overhead is intentionally over-estimated so the lease is
    # conservative on both CPython and Windows builds.
    return 512 + bone_count * 224 + len(pose.track_samples) * 96


def estimate_batch_bytes(
    spec: Spec,
    batch_frames: int,
    track_bytes: int = 0,
    sampled_tracks: int = 0,
) -> int:
    per_pose = _pose_bytes(ClipPose(world={}, track_samples=()), len(spec.skeleton))
    per_pose += sampled_tracks * 96
    return 4096 + batch_frames * per_pose + track_bytes


def _track_buffer_bytes(spec: Spec, clip_id: str) -> int:
    total = 0
    clip = spec.clips[clip_id]
    for channels in clip.tracks.values():
        for track in channels.values():
            total += 128
            for key in track.keyframes:
                total += 64 + 8 * len(key.value)
    return total


def _sampled_track_count(spec: Spec, clip_id: str) -> int:
    count = 0
    for channels in spec.clips[clip_id].tracks.values():
        for track in channels.values():
            if track.keyframes:
                count += 1
    return count


def _run_batch(
    spec: Spec,
    clip_id: str,
    batch: int,
    offsets: tuple[int, ...],
    include_final: bool,
    budget: PriorityMemoryBudget,
) -> tuple[str, int, Mapping[int, ClipPose], ClipPose | None, int]:
    lease = estimate_batch_bytes(
        spec,
        len(offsets),
        _track_buffer_bytes(spec, clip_id),
        _sampled_track_count(spec, clip_id),
    )
    budget.acquire(clip_id, batch, lease)
    try:
        result = evaluate_clip_offsets(spec, clip_id, offsets, include_final=include_final)
        final = result.final_pose if include_final else None
        return clip_id, batch, result.poses, final, lease
    finally:
        budget.release(lease)


def precompute_clip_cache(
    spec: Spec,
    *,
    max_workers: int,
    memory_budget_bytes: int,
) -> tuple[dict[str, ClipPoseSet], dict[str, Any]]:
    if max_workers < 1:
        raise BudgetError("worker count must be at least one", details={"workers": max_workers})
    budget = PriorityMemoryBudget(memory_budget_bytes)
    step = spec.timeline.frame_step_ms

    clip_track_bytes = {clip_id: _track_buffer_bytes(spec, clip_id) for clip_id in spec.clips}
    max_sampled_tracks = max((_sampled_track_count(spec, clip_id) for clip_id in spec.clips), default=0)
    per_pose = _pose_bytes(ClipPose(world={}, track_samples=()), len(spec.skeleton))
    per_pose += max_sampled_tracks * 96
    minimum_batch = max(
        1,
        (memory_budget_bytes - max(clip_track_bytes.values(), default=0) - 4096) // max(1, per_pose),
    )

    tasks: list[tuple[str, int, tuple[int, ...], bool]] = []
    for clip_id in sorted(spec.clips):
        offsets = clip_local_offsets(spec.clips[clip_id], step)
        if not offsets:
            offsets = (0,)
        for start in range(0, len(offsets), minimum_batch):
            batch_offsets = offsets[start : start + minimum_batch]
            include_final = start + minimum_batch >= len(offsets)
            tasks.append((clip_id, len(tasks), batch_offsets, include_final))

    poses_by_clip: dict[str, dict[int, ClipPose]] = {clip_id: {} for clip_id in spec.clips}
    final_by_clip: dict[str, ClipPose] = {}
    leases: dict[str, int] = {clip_id: 0 for clip_id in spec.clips}
    task_count_by_clip: dict[str, int] = {}
    futures: dict[Future[Any], tuple[str, int, tuple[int, ...], bool]] = {}
    submitted = 0

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="anim-clip") as executor:
        while submitted < len(tasks) or futures:
            while submitted < len(tasks) and len(futures) < max_workers:
                clip_id, batch, offsets, include_final = tasks[submitted]
                contains_final = any(offset == spec.clips[clip_id].duration_ms for offset in offsets)
                del contains_final
                future = executor.submit(
                    _run_batch,
                    spec,
                    clip_id,
                    batch,
                    offsets,
                    include_final,
                    budget,
                )
                futures[future] = (clip_id, batch, offsets, include_final)
                task_count_by_clip[clip_id] = task_count_by_clip.get(clip_id, 0) + 1
                submitted += 1
            done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in done:
                clip_id, _batch, _offsets, include_final = futures.pop(future)
                result_clip, _batch_index, poses, final, lease = future.result()
                if result_clip != clip_id:
                    raise RuntimeError("clip cache result was associated with the wrong clip")
                poses_by_clip[clip_id].update(poses)
                leases[clip_id] += lease
                if include_final:
                    if final is None:
                        raise RuntimeError("final clip pose was not produced")
                    final_by_clip[clip_id] = final

    cache: dict[str, ClipPoseSet] = {}
    retained_bytes = 0
    for clip_id in sorted(spec.clips):
        clip = spec.clips[clip_id]
        if clip_id not in final_by_clip:
            final_result = evaluate_clip_offsets(spec, clip_id, (), include_final=True)
            final_by_clip[clip_id] = final_result.final_pose
        poses = poses_by_clip[clip_id]
        clip_retained = sum(_pose_bytes(pose, len(spec.skeleton)) for pose in poses.values())
        clip_retained += _pose_bytes(final_by_clip[clip_id], len(spec.skeleton))
        retained_bytes += clip_retained
        cache[clip_id] = ClipPoseSet(
            clip_id=clip_id,
            duration_ms=clip.duration_ms,
            step_ms=step,
            poses=poses,
            final_pose=final_by_clip[clip_id],
            estimated_bytes=clip_retained,
        )

    report = {
        "max_workers": max_workers,
        "memory_budget_bytes": memory_budget_bytes,
        "peak_inflight_bytes": budget.peak_bytes,
        "retained_pose_bytes": retained_bytes,
        "priority_waits": budget.waits,
        "tasks": len(tasks),
        "batch_clip_order": [task[0] for task in tasks],
        "tasks_per_clip": task_count_by_clip,
    }
    return cache, report

