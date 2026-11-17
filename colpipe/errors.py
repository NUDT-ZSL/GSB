"""Business errors with stable JSON rendering."""

from __future__ import annotations

from typing import Any


class PipeError(Exception):
    """An expected business failure.

    ``path`` points at the offending JSON/query field when one exists.  The
    loader is deliberately side-effect free, so raising this exception never
    leaves a partially populated catalog behind.
    """

    def __init__(
        self,
        code: str,
        message: str,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "FAIL",
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        suffix = f" at {self.path}" if self.path else ""
        return f"{self.code}: {self.message}{suffix}"

