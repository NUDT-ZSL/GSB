"""Stable business error codes used by all layers."""

from __future__ import annotations

from typing import Any


class LayoutError(Exception):
    """A user-visible, machine-readable pipeline failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        line: int | None = None,
        block_id: int | None = None,
        page_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.line = line
        self.block_id = block_id
        self.page_id = page_id
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.line is not None:
            payload["line"] = self.line
        if self.block_id is not None:
            payload["block_id"] = self.block_id
        if self.page_id is not None:
            payload["page_id"] = self.page_id
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        location = []
        if self.page_id is not None:
            location.append(f"page={self.page_id}")
        if self.block_id is not None:
            location.append(f"block={self.block_id}")
        if self.field is not None:
            location.append(f"field={self.field}")
        if self.line is not None:
            location.append(f"line={self.line}")
        suffix = f" ({', '.join(location)})" if location else ""
        return f"{self.code}: {self.message}{suffix}"


def error_payload(exc: LayoutError) -> dict[str, Any]:
    return exc.to_dict()

