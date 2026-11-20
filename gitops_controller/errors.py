"""Business errors exposed by the controller and CLI."""

from __future__ import annotations

from typing import Any


class GitOpsError(Exception):
    """An expected error with a stable, machine-readable business code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "details": self.details,
            "message": self.message,
        }


class DuplicateKeyError(ValueError):
    """Raised by the strict JSON loader when an object repeats a key."""

