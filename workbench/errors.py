from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    stage: str = "unknown"
    sample_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        prefix = f"{self.code}"
        if self.sample_id:
            prefix += f" [{self.sample_id}]"
        return f"{prefix}: {self.message}"

    def as_failure(self, exception_type: str | None = None) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "stage": self.stage,
            "business_code": self.code,
            "exception_type": exception_type or type(self).__name__,
            "message": self.message,
            "details": self.details,
        }


def failure_from_exception(
    exc: BaseException,
    *,
    stage: str,
    sample_id: str | None,
    fallback_code: str = "E_UNEXPECTED",
) -> dict[str, Any]:
    if isinstance(exc, WorkbenchError):
        failure = exc.as_failure(type(exc).__name__)
        failure["sample_id"] = failure["sample_id"] or sample_id or "<unknown>"
        failure["stage"] = stage
        return failure
    return {
        "sample_id": sample_id or "<unknown>",
        "stage": stage,
        "business_code": fallback_code,
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "details": {},
    }
