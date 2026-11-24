from __future__ import annotations

from typing import Any


class BalanceLabError(Exception):
    """Base class for business errors with a stable E_* code."""

    code = "E_INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class ValidationError(BalanceLabError):
    code = "E_VALIDATION"


class BudgetError(BalanceLabError):
    code = "E_BUDGET"


class LedgerError(BalanceLabError):
    code = "E_LEDGER"


class ReportVersionError(BalanceLabError):
    code = "E_REPORT_VERSION"


def error_envelope(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, BalanceLabError):
        return {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        }
    return {
        "ok": False,
        "error": {
            "code": "E_INTERNAL",
            "message": str(exc) or exc.__class__.__name__,
            "details": {"type": exc.__class__.__name__},
        },
    }
