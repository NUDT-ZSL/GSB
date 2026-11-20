"""Structured exceptions for integrable callers."""

from __future__ import annotations

from typing import Any

from .codes import BusinessCode


class AedError(Exception):
    """An expected validation or consistency failure.

    ``context`` is always JSON serializable and carries the dialog/field/event
    position needed by callers to locate the offending input.
    """

    def __init__(
        self,
        code: BusinessCode | str,
        message: str,
        *,
        dialog_id: str | None = None,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code.value if isinstance(code, BusinessCode) else str(code)
        self.message = message
        self.dialog_id = dialog_id
        self.field = field
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.dialog_id is not None:
            payload["dialog_id"] = self.dialog_id
        if self.field is not None:
            payload["field"] = self.field
        if self.details:
            payload["details"] = self.details
        return payload
