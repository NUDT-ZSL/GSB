"""Structured business errors used by every pipeline stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_INPUT = "E_INPUT"
E_CONFIG = "E_CONFIG"
E_TERM_UNKNOWN = "E_TERM_UNKNOWN"
E_TERM_INVALID = "E_TERM_INVALID"
E_DUPLICATE_ID = "E_DUPLICATE_ID"
E_WORKER = "E_WORKER"
E_COMPUTE_BUDGET = "E_COMPUTE_BUDGET"
E_MEMORY_BUDGET = "E_MEMORY_BUDGET"
E_SLOT_LIMIT = "E_SLOT_LIMIT"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_INCONSISTENT = "E_INCONSISTENT"


@dataclass
class RecallError(Exception):
    """A serializable failure attributed to a business stage and batch."""

    code: str
    stage: str
    message: str
    batch_no: int | None = None
    batch_kind: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    @property
    def batch_key(self) -> str | None:
        if self.batch_kind is None or self.batch_no is None:
            return None
        return f"{self.batch_kind}:{self.batch_no}"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
        }
        if self.batch_no is not None:
            result["batch_no"] = self.batch_no
        if self.batch_kind is not None:
            result["batch_kind"] = self.batch_kind
        if self.batch_key is not None:
            result["batch_key"] = self.batch_key
        if self.details:
            result["details"] = self.details
        return result


def error_dict(
    code: str,
    stage: str,
    message: str,
    *,
    batch_no: int | None = None,
    batch_kind: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return RecallError(
        code=code,
        stage=stage,
        message=message,
        batch_no=batch_no,
        batch_kind=batch_kind,
        details=details or {},
    ).to_dict()
