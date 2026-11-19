"""Business errors with stable machine-readable codes."""

from __future__ import annotations

from typing import Any


E_OK = "E_OK"
E_BAD_SAMPLE = "E_BAD_SAMPLE"
E_BAD_INSTANCE = "E_BAD_INSTANCE"
E_BAD_PLAN = "E_BAD_PLAN"
E_BAD_DIAGNOSTICS = "E_BAD_DIAGNOSTICS"
E_SAMPLE_MISMATCH = "E_SAMPLE_MISMATCH"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
E_CONN_REFUSED = "E_CONN_REFUSED"
E_TIMEOUT = "E_TIMEOUT"
E_STATUS_MISMATCH = "E_STATUS_MISMATCH"
E_MALFORMED_RESPONSE = "E_MALFORMED_RESPONSE"
E_PROBE_IO = "E_PROBE_IO"
E_SAMPLE_CONFLICT = "E_SAMPLE_CONFLICT"
E_CHECK_CONFLICT = "E_CHECK_CONFLICT"
E_WINDOW_CONFLICT = "E_WINDOW_CONFLICT"
E_STATE_INCONSISTENT = "E_STATE_INCONSISTENT"
E_DB_ERROR = "E_DB_ERROR"
E_VERIFY_FAILED = "E_VERIFY_FAILED"
E_INTERNAL = "E_INTERNAL"


class BusinessError(Exception):
    """An expected validation, persistence, or diagnostic failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fields: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields or {}
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "fields": self.fields,
            },
        }


def fail(
    code: str,
    message: str,
    *,
    fields: dict[str, Any] | None = None,
    exit_code: int = 2,
) -> None:
    raise BusinessError(code, message, fields=fields, exit_code=exit_code)


def as_business_error(exc: BaseException) -> BusinessError:
    if isinstance(exc, BusinessError):
        return exc
    return BusinessError(
        E_INTERNAL,
        f"{type(exc).__name__}: {exc}",
        exit_code=3,
    )

