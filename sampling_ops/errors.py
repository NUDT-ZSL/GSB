"""Structured business errors used across the four-stage pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


BUSINESS_CODES = {
    "E_DTYPE_UNSUPPORTED",
    "E_SHAPE_INVALID",
    "E_K_INVALID",
    "E_K_MODE_INVALID",
    "E_TIE_POLICY_UNSUPPORTED",
    "E_LOGITS_NON_FINITE",
    "E_TOPK_INCONSISTENT",
    "E_TEMPERATURE_INVALID",
    "E_TOP_P_INVALID",
    "E_SEED_INVALID",
    "E_GROUP_SIZE_INVALID",
    "E_RESOURCE_PROFILE_INVALID",
    "E_TOLERANCE_INVALID",
    "E_TOLERANCE_EXCEEDED",
    "E_INCONSISTENT",
    "E_INJECTION_INVALID",
    "E_SNAPSHOT_INVALID",
    "E_SNAPSHOT_VERSION_REGRESSION",
    "E_INTERNAL",
}


@dataclass
class BusinessError(Exception):
    """A failure that can be safely surfaced to an integrating caller."""

    code: str
    message: str
    field: str | None = None
    case_id: str | None = None
    stage: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    injected: bool = False

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)
        if self.code not in BUSINESS_CODES:
            raise ValueError(f"unknown business code: {self.code}")

    def to_record(
        self,
        *,
        stage: str | None = None,
        case_id: str | None = None,
        injected: bool | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "case_id": case_id if case_id is not None else self.case_id,
            "stage": stage if stage is not None else self.stage,
            "business_code": self.code,
            "field": self.field,
            "message": self.message,
            "injected": self.injected if injected is None else injected,
        }
        if self.details:
            record["details"] = self.details
        return record


def fail(
    code: str,
    message: str,
    *,
    field: str | None = None,
    case_id: str | None = None,
    stage: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    raise BusinessError(
        code=code,
        message=message,
        field=field,
        case_id=case_id,
        stage=stage,
        details=details or {},
    )

