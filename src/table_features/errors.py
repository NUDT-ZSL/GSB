"""Structured business errors used by every pipeline stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_SLOT_REF_INVALID = "E_SLOT_REF_INVALID"
E_SLOT_DUP = "E_SLOT_DUP"
E_SELECT_TARGET_INVALID = "E_SELECT_TARGET_INVALID"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_UNCLASSIFIED = "E_UNCLASSIFIED"
E_ENCODE_VALUE_RANGE = "E_ENCODE_VALUE_RANGE"
E_STANDARDIZE_VALUE_INVALID = "E_STANDARDIZE_VALUE_INVALID"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


@dataclass(frozen=True)
class ErrorRecord:
    """A failure attributed to one stage, feature, and optional row."""

    stage: str
    code: str
    message: str
    feature: str | None = None
    row_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "feature": self.feature,
            "row_id": self.row_id,
            "details": _json_safe(self.details),
        }


class BusinessError(Exception):
    """A deliberately classified failure that can be attributed by stage."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        feature: str | None = None,
        row_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.record = ErrorRecord(
            stage=stage,
            code=code,
            message=message,
            feature=feature,
            row_id=row_id,
            details=details or {},
        )

    @property
    def code(self) -> str:
        return self.record.code


@dataclass(frozen=True)
class Degradation:
    """A documented fallback caused by an upstream business code."""

    stage: str
    category: str
    triggered_by: str
    feature: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "category": self.category,
            "triggered_by": self.triggered_by,
            "feature": self.feature,
            "details": _json_safe(self.details),
        }

