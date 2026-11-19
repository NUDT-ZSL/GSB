"""Typed errors that preserve the stage, case and field for attribution."""

from __future__ import annotations

from typing import Any


class SparseOperatorError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        case_id: str | None = None,
        phase: str = "unknown",
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.case_id = case_id
        self.phase = phase
        self.field = field
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "business_code": self.code,
            "case_id": self.case_id,
            "phase": self.phase,
            "field": self.field,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload

