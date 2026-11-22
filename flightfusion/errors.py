"""Stable business error model used across all pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any


class ErrorCode:
    VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
    FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    AXIS_MAPPING_INVALID = "E_AXIS_MAPPING_INVALID"
    SAMPLE_RATE_INVALID = "E_SAMPLE_RATE_INVALID"
    CALIBRATION_ROLLBACK = "E_CALIBRATION_ROLLBACK"
    GAIN_OUT_OF_RANGE = "E_GAIN_OUT_OF_RANGE"
    WEIGHT_OUT_OF_RANGE = "E_WEIGHT_OUT_OF_RANGE"
    OUTPUT_FIELD_MISSING = "E_OUTPUT_FIELD_MISSING"
    UPSTREAM_DEGRADED = "E_UPSTREAM_DEGRADED"
    MIGRATION_FIELD_LOST = "E_MIGRATION_FIELD_LOST"
    CACHE_CORRUPT = "E_CACHE_CORRUPT"
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    STAGE_BUSY = "E_STAGE_BUSY"


@dataclass
class BusinessError(Exception):
    """A machine-readable domain error.

    ``stage`` is one of ``parse``, ``preprocess``, ``fusion`` or ``output``.
    ``field`` and ``line`` are intentionally retained for user-facing repair.
    """

    code: str
    message: str
    stage: str
    sensor_id: str | None = None
    field: str | None = None
    line: int | None = None
    value: Any = None
    degradation: str | None = None
    upstream_code: str | None = None
    details: dict[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "sensor_id": self.sensor_id,
            "field": self.field,
            "line": self.line,
            "value": self.value,
            "degradation": self.degradation,
            "upstream_code": self.upstream_code,
            "details": self.details,
        }


def diagnostic(
    *,
    code: str,
    stage: str,
    message: str,
    sensor_id: str | None = None,
    field: str | None = None,
    line: int | None = None,
    value: Any = None,
    degradation: str | None = None,
    upstream_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return BusinessError(
        code=code,
        message=message,
        stage=stage,
        sensor_id=sensor_id,
        field=field,
        line=line,
        value=value,
        degradation=degradation,
        upstream_code=upstream_code,
        details=details or {},
    ).to_dict()


def error_codes(items: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        code = str(item.get("code", ""))
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    return result

