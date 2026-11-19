"""Structured business errors used by every public entry point."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_FRAME_MISMATCH = "E_FRAME_MISMATCH"
E_SAMPLE_RATE_MISMATCH = "E_SAMPLE_RATE_MISMATCH"
E_SEED_SHAPE_MISMATCH = "E_SEED_SHAPE_MISMATCH"
E_REF_MISSING = "E_REF_MISSING"
E_DUPLICATE_UTTERANCE = "E_DUPLICATE_UTTERANCE"
E_PHONEME_OUT_OF_RANGE = "E_PHONEME_OUT_OF_RANGE"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_INCONSISTENT = "E_INCONSISTENT"


class DomainError(Exception):
    """A failure with a stable machine-readable business code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        utterance_id: str | None = None,
        field_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.utterance_id = utterance_id
        self.field_name = field_name
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "utterance_id": self.utterance_id,
            "field": self.field_name,
            "details": self.details,
        }


@dataclass
class DiagnosticFinding:
    code: str
    stage: str
    message: str
    utterance_id: str | None = None
    field_name: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_error(cls, exc: DomainError, stage: str | None = None) -> "DiagnosticFinding":
        return cls(
            code=exc.code,
            stage=stage or exc.stage or "unattributed",
            message=exc.message,
            utterance_id=exc.utterance_id,
            field_name=exc.field_name,
            details=dict(exc.details),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "utterance_id": self.utterance_id,
            "field": self.field_name,
            "message": self.message,
            "details": self.details,
        }


def require(condition: bool, code: str, message: str, **kwargs: Any) -> None:
    if not condition:
        raise DomainError(code, message, **kwargs)

