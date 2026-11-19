"""Stable business errors exposed by the command line interface."""

from __future__ import annotations

from typing import Any


class ColAggError(Exception):
    """An expected error with a stable machine-readable code."""

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

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


E_BAD_BATCH = "E_BAD_BATCH"
E_BAD_QUERY = "E_BAD_QUERY"
E_BAD_PRECISION = "E_BAD_PRECISION"
E_BAD_INPUT = "E_BAD_INPUT"
E_BATCH_DIGEST_MISMATCH = "E_BATCH_DIGEST_MISMATCH"
E_TYPE_MISMATCH = "E_TYPE_MISMATCH"
E_FIXED_OVERFLOW = "E_FIXED_OVERFLOW"
E_STATE_STALE = "E_STATE_STALE"
E_PLAN_CONFLICT = "E_PLAN_CONFLICT"
E_PLAN_ROLLBACK = "E_PLAN_ROLLBACK"
E_PLAN_CHANGED = "E_PLAN_CHANGED"
E_NO_STATE = "E_NO_STATE"
E_NOT_FOUND = "E_NOT_FOUND"
E_INCOMPLETE = "E_INCOMPLETE"
E_STORAGE = "E_STORAGE"
E_VERIFY_FAILED = "E_VERIFY_FAILED"
E_INTERNAL = "E_INTERNAL"

