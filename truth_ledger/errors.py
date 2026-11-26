from __future__ import annotations

from typing import Any


class LedgerError(Exception):
    """Structured business error used across all pipeline stages."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.details = details or {}

    def to_record(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "stage": self.stage or self.details.get("stage"),
            "message": self.message,
            "details": self.details,
        }


def error_record(exc: BaseException, *, stage: str | None = None) -> dict[str, Any]:
    if isinstance(exc, LedgerError):
        record = exc.to_record()
        if stage and not record.get("stage"):
            record["stage"] = stage
        return record
    return {
        "ok": False,
        "code": "E_INTERNAL",
        "stage": stage,
        "message": f"{type(exc).__name__}: {exc}",
        "details": {},
    }
