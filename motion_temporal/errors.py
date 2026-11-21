"""Business exceptions with stable machine-readable context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class BusinessError(Exception):
    code: str
    message: str
    clip_id: str | None = None
    frame_index: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    located_frames: list[int] = field(default_factory=list)
    stage: str | None = None

    def __str__(self) -> str:
        location = f" clip={self.clip_id} frame={self.frame_index}"
        return f"{self.code}:{location} {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "clip_id": self.clip_id,
            "frame_index": self.frame_index,
            "stage": self.stage,
            "details": self.details,
            "located_frames": self.located_frames,
        }


class GracefulStop(Exception):
    """A requested frame-boundary stop, not a business failure."""

    def __init__(self, stage: str, frame_index: int, clip_id: str):
        super().__init__(f"stop requested at {stage}:{frame_index}")
        self.stage = stage
        self.frame_index = frame_index
        self.clip_id = clip_id


def invalid_input(message: str, clip_id: str | None = None, frame_index: int | None = None) -> BusinessError:
    return BusinessError(E_INPUT_INVALID, message, clip_id, frame_index)

