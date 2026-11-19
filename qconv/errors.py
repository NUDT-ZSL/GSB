from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    E_DUPLICATE_JOB_ID = "E_DUPLICATE_JOB_ID"
    E_INVALID_INPUT_SHAPE = "E_INVALID_INPUT_SHAPE"
    E_INVALID_KERNEL_SHAPE = "E_INVALID_KERNEL_SHAPE"
    E_INVALID_STRIDE_PADDING = "E_INVALID_STRIDE_PADDING"
    E_INVALID_QUANT_SCALE = "E_INVALID_QUANT_SCALE"
    E_INVALID_ZERO_POINT = "E_INVALID_ZERO_POINT"
    E_UNSUPPORTED_ACC_DTYPE = "E_UNSUPPORTED_ACC_DTYPE"
    E_INVALID_JOB_FIELD = "E_INVALID_JOB_FIELD"
    E_INVALID_WORKER_CONFIG = "E_INVALID_WORKER_CONFIG"
    E_INVALID_BUDGET = "E_INVALID_BUDGET"
    E_MAC_BUDGET = "E_MAC_BUDGET"
    E_MEMORY_LIMIT = "E_MEMORY_LIMIT"
    E_WORKER_SLOTS = "E_WORKER_SLOTS"
    E_ACCUMULATOR_OVERFLOW = "E_ACCUMULATOR_OVERFLOW"
    E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
    E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    E_STAGE_BUSY = "E_STAGE_BUSY"
    E_INCONSISTENT = "E_INCONSISTENT"
    E_WORKER_FAILURE = "E_WORKER_FAILURE"
    E_INJECTED_FAULT = "E_INJECTED_FAULT"
    E_NOT_FOUND = "E_NOT_FOUND"


@dataclass
class BusinessIssue:
    code: ErrorCode | str
    message: str
    job_id: str | None = None
    chunk_id: int | None = None
    stage: str | None = None
    field: str | None = None
    detail: dict[str, Any] = dataclass_field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "message": self.message,
            "job_id": self.job_id,
            "chunk_id": self.chunk_id,
            "stage": self.stage,
            "field": self.field,
            "detail": self.detail,
        }


class DomainError(Exception):
    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        job_id: str | None = None,
        chunk_id: int | None = None,
        stage: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = ErrorCode(code) if str(code) in ErrorCode._value2member_map_ else str(code)
        self.message = message
        self.job_id = job_id
        self.chunk_id = chunk_id
        self.stage = stage
        self.detail = detail or {}

    def as_issue(self) -> BusinessIssue:
        return BusinessIssue(
            code=self.code,
            message=self.message,
            job_id=self.job_id,
            chunk_id=self.chunk_id,
            stage=self.stage,
            detail=self.detail,
        )

    def as_dict(self) -> dict[str, Any]:
        return self.as_issue().as_dict() | {"detail": self.detail}


def code_value(code: ErrorCode | str) -> str:
    return code.value if isinstance(code, ErrorCode) else str(code)

