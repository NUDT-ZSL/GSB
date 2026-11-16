"""Business error codes and pipeline exceptions.

All failure paths inside the pipeline raise :class:`PipelineError` carrying a
stable business code.  The business codes are part of the validation report
contract and must never be renamed.
"""

from __future__ import annotations

from typing import Any


E_RLE_OUT_OF_RANGE = "E_RLE_OUT_OF_RANGE"
E_MASK_LENGTH_MISMATCH = "E_MASK_LENGTH_MISMATCH"
E_PALETTE_MISSING = "E_PALETTE_MISSING"
E_BOUNDARY_SENSITIVE = "E_BOUNDARY_SENSITIVE"
E_INSTANCE_TASK_FAILED = "E_INSTANCE_TASK_FAILED"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_INPUT_INVALID = "E_INPUT_INVALID"
E_UNKNOWN_STAGE = "E_UNKNOWN_STAGE"
E_FAULT_INJECTED = "E_FAULT_INJECTED"

# Exit code contract for the run entry point.
EXIT_OK = 0
EXIT_HANDLED_SIGNAL = 0
EXIT_HARD_CRASH = 137
EXIT_BUSINESS_FAILURES = 2
EXIT_FATAL = 3


class PipelineError(Exception):
    """An error that is mapped to a deterministic business code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        instance_id: str | None = None,
        stage: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.instance_id = instance_id
        self.stage = stage
        self.details = details or {}

    def to_record(self, exception_type: str | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "exception_type": exception_type or type(self).__name__,
        }
        if self.instance_id is not None:
            record["instance_id"] = self.instance_id
        if self.stage is not None:
            record["stage"] = self.stage
        if self.details:
            record["details"] = self.details
        return record


E_HARD_CRASH_SIMULATED = "E_HARD_CRASH_SIMULATED"
