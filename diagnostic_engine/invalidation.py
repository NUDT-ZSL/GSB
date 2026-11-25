"""Dependency invalidation planning and set invariant checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import ErrorCode, DiagnosticError
from .models import ClipTask


@dataclass(frozen=True)
class ChangePlan:
    clip_id: str
    changed_fields: tuple[str, ...]
    change_point_frame: int
    affected_start_frame: int
    affected_frames: tuple[int, ...]
    reused_frames: tuple[int, ...]
    old_revision: int
    new_revision: int

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "changed_fields": list(self.changed_fields),
            "change_point_frame": self.change_point_frame,
            "affected_start_frame": self.affected_start_frame,
            "affected_frames": list(self.affected_frames),
            "reused_frames": list(self.reused_frames),
            "old_revision": self.old_revision,
            "new_revision": self.new_revision,
        }


def validate_recompute_reuse_partition(
    clip_id: str | None,
    total_frames: int,
    recomputed: Iterable[int],
    reused: Iterable[int],
) -> None:
    recomputed_set = {int(frame) for frame in recomputed}
    reused_set = {int(frame) for frame in reused}
    all_frames = set(range(total_frames))
    overlap = sorted(recomputed_set & reused_set)
    missing = sorted(all_frames - (recomputed_set | reused_set))
    extra = sorted((recomputed_set | reused_set) - all_frames)
    if overlap or missing or extra:
        raise DiagnosticError(
            ErrorCode.INVALIDATION_MISMATCH,
            "recomputed and reused frame sets must be disjoint and cover all frames",
            clip_id=clip_id,
            field="frame_sets",
            details={
                "overlap": overlap[:20],
                "missing": missing[:20],
                "extra": extra[:20],
                "overlap_count": len(overlap),
                "missing_count": len(missing),
                "extra_count": len(extra),
            },
        )


def plan_change(
    old_task: ClipTask,
    new_task: ClipTask,
    *,
    change_frame: int | None = None,
) -> ChangePlan:
    if old_task.clip_id != new_task.clip_id:
        raise DiagnosticError(
            ErrorCode.INPUT_INVALID,
            "cannot plan change across different clip_ids",
            clip_id=new_task.clip_id,
            field="clip_id",
        )
    if new_task.revision < old_task.revision:
        raise DiagnosticError(
            ErrorCode.REVISION_ROLLBACK,
            "revision rollback is not allowed",
            clip_id=new_task.clip_id,
            field="revision",
            details={"old_revision": old_task.revision, "new_revision": new_task.revision},
        )

    changed_fields: list[str] = []
    for field_name in ("noise_seed", "propagation_steps", "block_size", "motion_mode", "frame_shape"):
        if getattr(old_task, field_name) != getattr(new_task, field_name):
            changed_fields.append(field_name)
    if new_task.revision > old_task.revision:
        changed_fields.append("revision")
    if not changed_fields:
        changed_fields.append("revision")

    point = change_frame
    if point is None:
        point = new_task.change_frame
    if point is None:
        point = 0
    if not (0 <= point < new_task.total_frames):
        raise DiagnosticError(
            ErrorCode.INVALIDATION_MISMATCH,
            "change_frame is outside the frame sequence",
            clip_id=new_task.clip_id,
            field="change_frame",
            details={"change_frame": point, "total_frames": new_task.total_frames},
        )

    block_size = new_task.block_size
    if "block_size" in changed_fields or "frame_shape" in changed_fields:
        point = 0
    affected_start = (point // block_size) * block_size
    if affected_start >= new_task.total_frames:
        affected_start = 0
    affected = tuple(range(affected_start, new_task.total_frames))
    reused = tuple(range(0, affected_start))
    validate_recompute_reuse_partition(
        new_task.clip_id, new_task.total_frames, affected, reused
    )
    return ChangePlan(
        clip_id=new_task.clip_id,
        changed_fields=tuple(changed_fields),
        change_point_frame=point,
        affected_start_frame=affected_start,
        affected_frames=affected,
        reused_frames=reused,
        old_revision=old_task.revision,
        new_revision=new_task.revision,
    )
