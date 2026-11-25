"""Task manifest models and input validation."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from .errors import ErrorCode, DiagnosticError


MOTION_MODES = ("translate", "ripple", "diffuse")


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_positive_int(
    data: Mapping[str, Any], key: str, clip_id: str | None
) -> int:
    value = data.get(key)
    if not _is_plain_int(value) or value <= 0:
        raise DiagnosticError(
            ErrorCode.PROPAGATION_INVALID,
            f"{key} must be a positive integer",
            clip_id=clip_id,
            field=key,
            details={"value": value},
        )
    return value


@dataclass(frozen=True)
class ClipTask:
    clip_id: str
    total_frames: int
    frame_shape: tuple[int, ...]
    noise_seed: int
    motion_mode: str
    block_size: int
    propagation_steps: int
    revision: int = 1
    change_frame: int | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClipTask":
        raw_clip_id = data.get("clip_id")
        clip_id = raw_clip_id if isinstance(raw_clip_id, str) and raw_clip_id else None
        if clip_id is None:
            raise DiagnosticError(
                ErrorCode.INPUT_INVALID,
                "clip_id must be a non-empty string",
                field="clip_id",
                details={"value": raw_clip_id},
            )

        total_frames = data.get("total_frames")
        if not _is_plain_int(total_frames) or total_frames <= 0:
            raise DiagnosticError(
                ErrorCode.INPUT_INVALID,
                "total_frames must be a positive integer",
                clip_id=clip_id,
                field="total_frames",
                details={"value": total_frames},
            )

        raw_shape = data.get("frame_shape")
        if not isinstance(raw_shape, (list, tuple)) or not raw_shape:
            raise DiagnosticError(
                ErrorCode.INPUT_INVALID,
                "frame_shape must be a non-empty array of positive dimensions",
                clip_id=clip_id,
                field="frame_shape",
                details={"value": raw_shape},
            )
        shape: list[int] = []
        for dimension in raw_shape:
            if not _is_plain_int(dimension) or dimension <= 0:
                raise DiagnosticError(
                    ErrorCode.INPUT_INVALID,
                    "frame_shape dimensions must be positive integers",
                    clip_id=clip_id,
                    field="frame_shape",
                    details={"value": list(raw_shape)},
                )
            shape.append(dimension)
        if len(shape) not in (2, 3):
            raise DiagnosticError(
                ErrorCode.INPUT_INVALID,
                "frame_shape must have two or three dimensions",
                clip_id=clip_id,
                field="frame_shape",
                details={"value": shape},
            )

        noise_seed = data.get("noise_seed")
        if not _is_plain_int(noise_seed) or noise_seed < 0:
            raise DiagnosticError(
                ErrorCode.INPUT_INVALID,
                "noise_seed must be a non-negative integer",
                clip_id=clip_id,
                field="noise_seed",
                details={"value": noise_seed},
            )

        motion_mode = data.get("motion_mode")
        if motion_mode not in MOTION_MODES:
            raise DiagnosticError(
                ErrorCode.PROPAGATION_INVALID,
                f"motion_mode must be one of {', '.join(MOTION_MODES)}",
                clip_id=clip_id,
                field="motion_mode",
                details={"value": motion_mode, "allowed": list(MOTION_MODES)},
            )

        block_size = _require_positive_int(data, "block_size", clip_id)
        propagation_steps = _require_positive_int(
            data, "propagation_steps", clip_id
        )

        revision = data.get("revision", 1)
        if not _is_plain_int(revision) or revision < 0:
            raise DiagnosticError(
                ErrorCode.INPUT_INVALID,
                "revision must be a non-negative integer",
                clip_id=clip_id,
                field="revision",
                details={"value": revision},
            )

        change_frame = data.get("change_frame")
        if change_frame is not None:
            if not _is_plain_int(change_frame) or not (
                0 <= change_frame < total_frames
            ):
                raise DiagnosticError(
                    ErrorCode.INPUT_INVALID,
                    "change_frame must be in [0, total_frames)",
                    clip_id=clip_id,
                    field="change_frame",
                    details={"value": change_frame, "total_frames": total_frames},
                )

        return cls(
            clip_id=clip_id,
            total_frames=total_frames,
            frame_shape=tuple(shape),
            noise_seed=noise_seed,
            motion_mode=motion_mode,
            block_size=block_size,
            propagation_steps=propagation_steps,
            revision=revision,
            change_frame=change_frame,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["frame_shape"] = list(self.frame_shape)
        return payload

    @property
    def frame_elements(self) -> int:
        result = 1
        for dimension in self.frame_shape:
            result *= dimension
        return result

    @property
    def frame_pixels(self) -> int:
        """Spatial pixel count, excluding a channel dimension."""
        if len(self.frame_shape) == 3:
            return self.frame_shape[0] * self.frame_shape[1]
        return self.frame_shape[0] * self.frame_shape[1]


@dataclass
class ManifestValidation:
    tasks: list[ClipTask] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        if self.errors:
            raise DiagnosticError(
                self.errors[0]["code"],
                self.errors[0]["message"],
                clip_id=self.errors[0].get("clip_id"),
                field=self.errors[0].get("field"),
                details={"issues": self.errors},
            )


def validate_manifest_records(records: Iterable[Mapping[str, Any]]) -> ManifestValidation:
    result = ManifestValidation()
    seen: set[str] = set()
    for line_number, record in enumerate(records, start=1):
        clip_id = record.get("clip_id") if isinstance(record, Mapping) else None
        if isinstance(clip_id, str) and clip_id in seen:
            result.errors.append(
                DiagnosticError(
                    ErrorCode.DUPLICATE_CLIP_ID,
                    "duplicate clip_id in manifest",
                    clip_id=clip_id,
                    field="clip_id",
                    details={"line": line_number},
                ).to_dict()
            )
            continue
        try:
            task = ClipTask.from_dict(record)
        except DiagnosticError as error:
            payload = error.to_dict()
            payload.setdefault("details", {})["line"] = line_number
            result.errors.append(payload)
            continue
        seen.add(task.clip_id)
        result.tasks.append(task)
    return result


def load_manifest(path: str | Path) -> ManifestValidation:
    records: list[Mapping[str, Any]] = []
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as error:
                validation = ManifestValidation()
                validation.errors.append(
                    DiagnosticError(
                        ErrorCode.JSONL_INVALID,
                        "invalid JSONL record",
                        field="line",
                        details={"line": line_number, "message": str(error)},
                    ).to_dict()
                )
                return validation
            if not isinstance(record, Mapping):
                validation = ManifestValidation()
                validation.errors.append(
                    DiagnosticError(
                        ErrorCode.JSONL_INVALID,
                        "JSONL record must be an object",
                        field="line",
                        details={"line": line_number},
                    ).to_dict()
                )
                return validation
            records.append(record)
    return validate_manifest_records(records)


def validate_noise_array(task: ClipTask, noise: np.ndarray) -> None:
    expected_shape = (task.total_frames, *task.frame_shape)
    actual_shape = tuple(int(value) for value in noise.shape)
    if actual_shape != expected_shape:
        raise DiagnosticError(
            ErrorCode.NOISE_SHAPE_MISMATCH,
            "noise array dimensions do not match total_frames and frame_shape",
            clip_id=task.clip_id,
            field="noise",
            details={"expected_shape": list(expected_shape), "actual_shape": list(actual_shape)},
        )


def validate_revision_not_rollback(
    new_task: ClipTask, previous: ClipTask | Mapping[str, Any] | None
) -> None:
    if previous is None:
        return
    old_revision = previous.revision if isinstance(previous, ClipTask) else previous.get("revision")
    if _is_plain_int(old_revision) and new_task.revision < int(old_revision):
        raise DiagnosticError(
            ErrorCode.REVISION_ROLLBACK,
            "revision rollback is not allowed",
            clip_id=new_task.clip_id,
            field="revision",
            details={"old_revision": old_revision, "new_revision": new_task.revision},
        )
