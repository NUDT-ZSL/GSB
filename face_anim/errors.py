"""Structured, location-aware pipeline errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PipelineError(Exception):
    code: str
    message: str
    path: str | None = None
    version: str | None = None
    sentence_id: str | None = None
    phoneme_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        location = []
        if self.path:
            location.append(f"path={self.path}")
        if self.version:
            location.append(f"version={self.version}")
        if self.sentence_id:
            location.append(f"sentence={self.sentence_id}")
        if self.phoneme_id:
            location.append(f"phoneme={self.phoneme_id}")
        suffix = f" ({', '.join(location)})" if location else ""
        return f"[{self.code}] {self.message}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "museum-lipsync-error/v1",
            "status": "error",
            "error": {
                "code": self.code,
                "message": self.message,
                "location": {
                    "path": self.path,
                    "version": self.version,
                    "sentence_id": self.sentence_id,
                    "phoneme_id": self.phoneme_id,
                },
                "details": self.details,
            },
        }


def fail(
    code: str,
    message: str,
    *,
    path: str | None = None,
    version: str | None = None,
    sentence_id: str | None = None,
    phoneme_id: str | None = None,
    **details: Any,
) -> None:
    raise PipelineError(
        code=code,
        message=message,
        path=path,
        version=version,
        sentence_id=sentence_id,
        phoneme_id=phoneme_id,
        details=details,
    )
