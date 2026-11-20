from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_CODES = {
    "E_DUPLICATE_SEQ_ID",
    "E_FRAME_GAP",
    "E_DUPLICATE_DETECTION",
    "E_INVALID_BBOX",
    "E_INVALID_OCCLUDED",
    "E_INVALID_SCORE",
    "E_INVALID_INPUT",
    "E_TRACK_ILLEGAL",
    "E_GAP_INVALID",
    "E_INCONSISTENT",
    "E_RESOURCE_LIMIT",
    "E_CHECKPOINT_CORRUPT",
    "E_REVISION_CONFLICT",
    "E_REVISION_INVALID",
}


@dataclass
class TrackingError(Exception):
    code: str
    message: str
    seq_id: str | None = None
    frame_index: int | None = None
    category: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown business error code: {self.code}")
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "seq_id": self.seq_id,
            "frame_index": self.frame_index,
            "category": self.category,
        }
        if self.context:
            payload["context"] = self.context
        return payload

    def __str__(self) -> str:
        location = []
        if self.seq_id is not None:
            location.append(f"seq={self.seq_id}")
        if self.frame_index is not None:
            location.append(f"frame={self.frame_index}")
        suffix = f" ({', '.join(location)})" if location else ""
        return f"{self.code}: {self.message}{suffix}"
