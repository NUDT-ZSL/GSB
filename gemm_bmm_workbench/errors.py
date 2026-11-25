from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class Codes:
    SCHEMA_INVALID = "E_SCHEMA_INVALID"
    SHAPE_INVALID = "E_SHAPE_INVALID"
    DTYPE_UNSUPPORTED = "E_DTYPE_UNSUPPORTED"
    LAYOUT_INVALID = "E_LAYOUT_INVALID"
    BLOCK_INVALID = "E_BLOCK_INVALID"
    TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
    JOB_DUPLICATE = "E_JOB_DUPLICATE"
    REVISION_REGRESSION = "E_REVISION_REGRESSION"
    LEDGER_VERSION_REGRESSION = "E_LEDGER_VERSION_REGRESSION"
    STAGE_BUSY = "E_STAGE_BUSY"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    SELFCHECK_FAILED = "E_SELFCHECK_FAILED"
    NOT_FOUND = "E_NOT_FOUND"
    STATE_INVALID = "E_STATE_INVALID"
    FAULT_INJECTED = "E_FAULT_INJECTED"


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None
    http_status: int = 400

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "details": self.details or {},
        }


def error_record(exc: BaseException | WorkbenchError, *, source: str = "runtime") -> dict[str, Any]:
    if isinstance(exc, WorkbenchError):
        record = exc.to_dict()
    else:
        record = {
            "ok": False,
            "code": "E_INTERNAL",
            "message": str(exc) or exc.__class__.__name__,
            "details": {"type": exc.__class__.__name__},
        }
    record["source"] = source
    return record
