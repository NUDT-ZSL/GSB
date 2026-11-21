"""Structured business errors used across the workbench."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


E_CONFIG_WEIGHT = "E_CONFIG_WEIGHT"
E_CONFIG_THRESHOLD = "E_CONFIG_THRESHOLD"
E_CONFIG_SMOOTHING = "E_CONFIG_SMOOTHING"
E_CONFIG_PRECISION = "E_CONFIG_PRECISION"
E_CONFIG_FIXED_SCALE = "E_CONFIG_FIXED_SCALE"
E_CONFIG_ROUNDING = "E_CONFIG_ROUNDING"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_SAMPLE_DUPLICATE_ID = "E_SAMPLE_DUPLICATE_ID"
E_SAMPLE_EMPTY_FIELD = "E_SAMPLE_EMPTY_FIELD"
E_SAMPLE_INVALID = "E_SAMPLE_INVALID"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_VERSION = "E_SNAPSHOT_VERSION"
E_INTERNAL = "E_INTERNAL"


@dataclass(frozen=True)
class BusinessError(Exception):
    """A machine-readable failure that is safe to surface in the local UI."""

    code: str
    message: str
    field: str | None = None
    pair_id: str | None = None
    stage: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __str__(self) -> str:
        location = []
        if self.stage:
            location.append(f"stage={self.stage}")
        if self.field:
            location.append(f"field={self.field}")
        if self.pair_id:
            location.append(f"pair_id={self.pair_id}")
        suffix = f" ({', '.join(location)})" if location else ""
        return f"{self.code}: {self.message}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "pair_id": self.pair_id,
            "stage": self.stage,
            "details": self.details,
        }


def as_error(value: Any) -> BusinessError:
    if isinstance(value, BusinessError):
        return value
    return BusinessError(E_INTERNAL, str(value))

