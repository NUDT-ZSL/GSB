"""Domain errors shared by the CLI, pipeline and local web application."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MeetingError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def corrupt(message: str, *, line: int, field_name: str, **details: Any) -> MeetingError:
    return MeetingError(
        "E_FORMAT_CORRUPT",
        message,
        {"line": line, "field": field_name, **details},
    )


def unsupported_version(version: str, *, line: int) -> MeetingError:
    return MeetingError(
        "E_VERSION_UNSUPPORTED",
        f"unsupported configuration version: {version}",
        {"version": version, "line": line},
    )


def timebase_mismatch(message: str, *, segment_id: str, start_frame: int, end_frame: int, **details: Any) -> MeetingError:
    return MeetingError(
        "E_TIME_BASE_MISMATCH",
        message,
        {
            "segment_id": segment_id,
            "frame_interval": [start_frame, end_frame],
            **details,
        },
    )


def unknown_speaker(speaker_id: int, allowed: list[int]) -> MeetingError:
    return MeetingError(
        "E_SPEAKER_UNKNOWN",
        f"speaker {speaker_id} is outside the session speaker set",
        {"speaker_id": speaker_id, "allowed_speaker_ids": allowed},
    )
