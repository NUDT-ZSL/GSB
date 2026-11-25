from __future__ import annotations

from typing import Any, Optional


class CacheLedgerError(Exception):
    """Business error with a stable machine-readable code."""

    exit_code = 2

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
        exit_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = details or {}
        if exit_code is not None:
            self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            error["field"] = self.field
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


class ClockRollbackError(CacheLedgerError):
    exit_code = 10

    def __init__(self, requested_clock_ms: int, watermark_ms: int, tolerance_ms: int) -> None:
        super().__init__(
            "E_CLOCK_ROLLBACK",
            "injected logical clock moved backwards beyond tolerance",
            field="clock_ms",
            details={
                "requested_clock_ms": requested_clock_ms,
                "watermark_ms": watermark_ms,
                "tolerance_ms": tolerance_ms,
                "backward_delta_ms": watermark_ms - requested_clock_ms,
            },
        )


class PolicyRollbackError(CacheLedgerError):
    exit_code = 11

    def __init__(self, committed_generation: int, incoming_generation: int) -> None:
        super().__init__(
            "E_POLICY_GENERATION_ROLLBACK",
            "policy generation cannot be replaced by an older generation",
            field="generation",
            details={
                "committed_generation": committed_generation,
                "incoming_generation": incoming_generation,
            },
        )


class VerificationError(CacheLedgerError):
    exit_code = 12

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__("E_VERIFY_MISMATCH", message, details=details, exit_code=12)
