"""Structured business errors used across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


@dataclass
class BusinessError(Exception):
    """An error that is safe to expose as a structured API/CLI response."""

    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        suffix = f" field={self.field}" if self.field else ""
        return f"{self.code}{suffix}: {self.message}"


def fail(
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    raise BusinessError(code, message, field=field, details=details or {})

