from __future__ import annotations

from typing import Any


HTTP_STATUS_BY_CODE = {
    "E_BAD_REQUEST": 400,
    "E_NOT_FOUND": 404,
    "E_VIEW_ROLLBACK": 409,
    "E_SNAPSHOT_VERSION_CONFLICT": 409,
    "E_PATCH_CONFLICT": 409,
    "E_EVENT_CONFLICT": 409,
    "E_CLOCK_ROLLBACK": 409,
    "E_NONDETERMINISTIC": 500,
}


class AlertLabError(Exception):
    """A business error with a stable code and optional structured details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Any | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.http_status = http_status or HTTP_STATUS_BY_CODE.get(code, 422)

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"ok": False, "error": error}


def invalid(
    code: str,
    message: str,
    *,
    http_status: int | None = None,
    **details: Any,
) -> AlertLabError:
    return AlertLabError(
        code,
        message,
        details=details or None,
        http_status=http_status,
    )
