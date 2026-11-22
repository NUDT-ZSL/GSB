"""Business error codes and structured exceptions."""

from __future__ import annotations

from typing import Any


class ErrorCode:
    BAD_PLAN = "E_BAD_PLAN"
    BAD_STATS = "E_BAD_STATS"
    TYPE_MISMATCH = "E_TYPE_MISMATCH"
    BATCH_DIGEST_MISMATCH = "E_BATCH_DIGEST_MISMATCH"
    EMPTY_OBSERVATION = "E_EMPTY_OBSERVATION"
    SWITCH_BUDGET_INVALID = "E_SWITCH_BUDGET_INVALID"
    SWITCH_BUDGET = "E_SWITCH_BUDGET"
    FORM_VERSION_REGRESSION = "E_FORM_VERSION_REGRESSION"
    WATERMARK_MISMATCH = "E_WATERMARK_MISMATCH"
    STATE_CORRUPT = "E_STATE_CORRUPT"
    CONCURRENCY_CONFLICT = "E_CONCURRENCY_CONFLICT"
    NOT_FOUND = "E_NOT_FOUND"
    VERIFY_MISMATCH = "E_VERIFY_MISMATCH"
    IO = "E_IO"
    BAD_COMMAND = "E_BAD_COMMAND"


class BusinessError(Exception):
    """An expected, diagnosable domain error.

    ``details`` is deliberately JSON-serializable so the CLI and callers can
    retain the complete diagnostic chain without parsing the message.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

