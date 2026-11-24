"""Business errors and validation issue records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Issue:
    """A non-fatal declaration rejection."""

    source_type: str
    record_index: int
    code: str
    message: str
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_type": self.source_type,
            "record_index": self.record_index,
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            result["field"] = self.field
        return result


class BusinessError(Exception):
    """A fatal, structured business error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }

