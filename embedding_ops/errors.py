"""Structured business errors used by every pipeline stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_INDEX_OUT_OF_RANGE = "E_INDEX_OUT_OF_RANGE"
E_INDEX_EMPTY = "E_INDEX_EMPTY"
E_INDEX_INVALID = "E_INDEX_INVALID"
E_PAD_INDEX_INVALID = "E_PAD_INDEX_INVALID"
E_QUANT_INVALID = "E_QUANT_INVALID"
E_SHAPE_MISMATCH = "E_SHAPE_MISMATCH"
E_SCALE_NEGATIVE = "E_SCALE_NEGATIVE"
E_DTYPE_UNKNOWN = "E_DTYPE_UNKNOWN"
E_DUP_JOB = "E_DUP_JOB"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_AXIS_INVALID = "E_AXIS_INVALID"
E_REDUCTION_MODE_UNKNOWN = "E_REDUCTION_MODE_UNKNOWN"
E_ROUNDING_MODE_UNKNOWN = "E_ROUNDING_MODE_UNKNOWN"
E_SUBNORMAL_POLICY_UNKNOWN = "E_SUBNORMAL_POLICY_UNKNOWN"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_MANIFEST_INVALID = "E_MANIFEST_INVALID"


@dataclass
class BusinessError(Exception):
    """A deterministic, attributable error returned across module boundaries."""

    code: str
    stage: str
    job_id: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "job_id": self.job_id,
            "message": self.message,
            "detail": self.detail,
        }


def business_error(
    code: str,
    stage: str,
    job_id: str,
    message: str,
    **detail: Any,
) -> BusinessError:
    return BusinessError(
        code=code,
        stage=stage,
        job_id=str(job_id),
        message=message,
        detail=detail,
    )
