from __future__ import annotations

from typing import Any


E_BAD_BATCH = "E_BAD_BATCH"
E_BAD_QUERY = "E_BAD_QUERY"
E_BAD_CONFIG = "E_BAD_CONFIG"
E_BAD_MANIFEST = "E_BAD_MANIFEST"
E_MALFORMED_COLUMN = "E_MALFORMED_COLUMN"
E_DIGEST_MISMATCH = "E_DIGEST_MISMATCH"
E_FIXED_OVERFLOW = "E_FIXED_OVERFLOW"
E_STATE_TOO_LARGE = "E_STATE_TOO_LARGE"
E_WINDOW_MISMATCH = "E_WINDOW_MISMATCH"
E_SPILL_TAMPERED = "E_SPILL_TAMPERED"
E_SPILL_READBACK_FAILED = "E_SPILL_READBACK_FAILED"
E_BATCH_OUT_OF_ORDER = "E_BATCH_OUT_OF_ORDER"
E_BATCH_CONFLICT = "E_BATCH_CONFLICT"
E_PLAN_CONFLICT = "E_PLAN_CONFLICT"
E_RECOVERY_FAILED = "E_RECOVERY_FAILED"
E_VERIFY_FAILED = "E_VERIFY_FAILED"
E_NOT_FOUND = "E_NOT_FOUND"
E_IO = "E_IO"


class EngineError(Exception):
    code: str
    message: str
    details: dict[str, Any]

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        **detail_fields: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.details.update(detail_fields)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def fail(code: str, message: str, **details: Any) -> "NoReturn":
    raise EngineError(code, message, details)
