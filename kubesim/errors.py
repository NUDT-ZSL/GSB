from __future__ import annotations

from typing import Any


class EngineError(Exception):
    """A deterministic, machine-readable business error."""

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
            "message": self.message,
            "details": self.details,
        }


class ValidationError(EngineError):
    pass


class StorageError(EngineError):
    pass


class ConflictError(EngineError):
    pass


def bad_node(message: str, **details: Any) -> ValidationError:
    return ValidationError("E_BAD_NODE", message, details)


def bad_pod(message: str, **details: Any) -> ValidationError:
    return ValidationError("E_BAD_POD", message, details)


def bad_config(message: str, **details: Any) -> ValidationError:
    return ValidationError("E_BAD_CONFIG", message, details)


def bad_injection(message: str, **details: Any) -> ValidationError:
    return ValidationError("E_BAD_INJECTION", message, details)
