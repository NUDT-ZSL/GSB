from __future__ import annotations

from typing import Any


ERROR_CODES = (
    "E_SHAPE_INVALID",
    "E_DTYPE_UNSUPPORTED",
    "E_PADDING_INVALID",
    "E_STRIDE_INVALID",
    "E_REVISION_ROLLBACK",
    "E_TOLERANCE_EXCEEDED",
    "E_STAGE_BUSY",
    "E_SNAPSHOT_ROLLBACK",
    "E_INCONSISTENT",
    "E_INTERP_MODE_UNSUPPORTED",
    "E_REVISION_CONFLICT",
    "E_MANIFEST_INVALID",
    "E_ARTIFACT_MISSING",
    "E_BLOCK_POLICY_INVALID",
)


class OpVerifyError(Exception):
    """Structured service error carrying a stable error code and field path."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        job_id: str | None = None,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.job_id = job_id
        self.field = field
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "job_id": self.job_id,
                "field": self.field,
                "details": self.details,
            },
        }


def error_response(
    code: str,
    message: str,
    *,
    job_id: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return OpVerifyError(
        code, message, job_id=job_id, field=field, details=details
    ).to_dict()
