"""Business errors and stable failure records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_GRAPH_INVALID = "E_GRAPH_INVALID"
E_PARAM_OUT_OF_RANGE = "E_PARAM_OUT_OF_RANGE"
E_SHAPE_MISMATCH = "E_SHAPE_MISMATCH"
E_NONFINITE_INPUT = "E_NONFINITE_INPUT"
E_DEPENDENCY_FAILED = "E_DEPENDENCY_FAILED"
E_NODE_DISABLED = "E_NODE_DISABLED"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_INPUT_INVALID = "E_INPUT_INVALID"
E_IO = "E_IO"
E_INTERRUPTED = "E_INTERRUPTED"
E_OPERATOR_FAILED = "E_OPERATOR_FAILED"
E_INTERNAL = "E_INTERNAL"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    node_id: str | None = None
    stage: str = "validation"
    details: dict[str, Any] = field(default_factory=dict)
    injection_id: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def exception_type(self) -> str:
        return type(self).__name__

    def to_failure(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "node_id": self.node_id,
            "stage": self.stage,
            "exception_type": self.exception_type,
        }
        if self.details:
            record["details"] = self.details
        if self.injection_id:
            record["injection_id"] = self.injection_id
        return record


def failure_from_exception(
    exc: BaseException,
    *,
    node_id: str | None,
    stage: str,
    injection_id: str | None = None,
) -> dict[str, Any]:
    if isinstance(exc, BusinessError):
        if node_id is not None and exc.node_id is None:
            exc.node_id = node_id
        exc.stage = stage
        if injection_id:
            exc.injection_id = injection_id
        return exc.to_failure()
    return {
        "code": E_OPERATOR_FAILED,
        "message": str(exc),
        "node_id": node_id,
        "stage": stage,
        "exception_type": type(exc).__name__,
        **({"injection_id": injection_id} if injection_id else {}),
    }
