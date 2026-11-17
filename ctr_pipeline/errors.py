"""Business error codes and exception types used by the pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


E_REQ_EMPTY = "E_REQ_EMPTY"
E_JSON_INVALID = "E_JSON_INVALID"
E_GROUP_UNKNOWN = "E_GROUP_UNKNOWN"
E_WIDTH_INVALID = "E_WIDTH_INVALID"
E_NON_FINITE = "E_NON_FINITE"
E_CANDIDATE_ID_EMPTY = "E_CANDIDATE_ID_EMPTY"
E_CANDIDATE_ID_DUPLICATE = "E_CANDIDATE_ID_DUPLICATE"
E_SHAPE_MISMATCH = "E_SHAPE_MISMATCH"
E_SPARSE_INVALID = "E_SPARSE_INVALID"
E_WEIGHT_SHAPE = "E_WEIGHT_SHAPE"
E_PARAM_INVALID = "E_PARAM_INVALID"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_INPUT_MISMATCH = "E_INPUT_MISMATCH"
E_STATE_INVALID = "E_STATE_INVALID"
E_REQ_QUOTA_EXCEEDED = "E_REQ_QUOTA_EXCEEDED"
E_BUDGET_EXHAUSTED = "E_BUDGET_EXHAUSTED"
E_INJECTION_INVALID = "E_INJECTION_INVALID"
E_INTERNAL = "E_INTERNAL"


@dataclass
class FailureRecord:
    request_id: str
    stage: str
    code: str
    exception_type: str
    message: str
    injection_ids: list[str]
    line_no: int | None = None
    candidate_id: str | None = None
    ready_queue_position: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "request_id": self.request_id,
            "stage": self.stage,
            "code": self.code,
            "exception_type": self.exception_type,
            "message": self.message,
            "injection_ids": sorted(self.injection_ids),
        }
        if self.line_no is not None:
            data["line_no"] = self.line_no
        if self.candidate_id is not None:
            data["candidate_id"] = self.candidate_id
        if self.ready_queue_position is not None:
            data["ready_queue_position"] = self.ready_queue_position
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FailureRecord":
        return cls(
            request_id=str(data["request_id"]),
            stage=str(data["stage"]),
            code=str(data["code"]),
            exception_type=str(data["exception_type"]),
            message=str(data["message"]),
            injection_ids=list(data.get("injection_ids", [])),
            line_no=data.get("line_no"),
            candidate_id=data.get("candidate_id"),
            ready_queue_position=data.get("ready_queue_position"),
        )


class PipelineError(Exception):
    """An expected, business-coded failure."""

    def __init__(self, code: str, stage: str, message: str, candidate_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message
        self.candidate_id = candidate_id


STAGE_ORDER = {
    "load": 0,
    "schedule": 1,
    "normalize": 2,
    "quantize": 3,
    "fusion": 4,
    "truncate": 5,
    "snapshot": 6,
    "report": 7,
}


def failure_sort_key(failure: FailureRecord | dict[str, Any]) -> tuple[Any, ...]:
    if isinstance(failure, FailureRecord):
        data = failure.to_dict()
    else:
        data = failure
    return (
        str(data.get("request_id", "")),
        STAGE_ORDER.get(str(data.get("stage", "")), 99),
        str(data.get("code", "")),
        str(data.get("candidate_id") or ""),
        tuple(data.get("injection_ids", [])),
        str(data.get("exception_type", "")),
        str(data.get("message", "")),
    )
