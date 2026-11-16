"""Domain errors with stable business codes."""

from __future__ import annotations

from typing import Any, Mapping


class DomainError(Exception):
    """Base class for errors that are safe to expose to API callers."""

    code = "E_DOMAIN"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class SLIValidationError(DomainError):
    """The complete SLI file was rejected."""

    code = "E_SLI"

    def __init__(
        self,
        message: str,
        *,
        line_no: int | None = None,
        field: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        if line_no is not None:
            merged["line"] = line_no
        if field is not None:
            merged["field"] = field
        super().__init__(message, details=merged)
        self.line_no = line_no
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["line"] = self.line_no
        payload["field"] = self.field
        return payload


class ConfigError(DomainError):
    """Configuration is malformed or internally inconsistent."""

    code = "E_CONFIG"


class SnapshotError(DomainError):
    """Snapshot cannot be parsed, validated, or written."""

    code = "E_SNAPSHOT"


class ReplayError(DomainError):
    """A snapshot cannot be reproduced bit-for-bit."""

    code = "E_REPLAY"

    def __init__(
        self,
        message: str,
        *,
        first_mismatch_step: int | None,
        field: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        merged["first_mismatch_step"] = first_mismatch_step
        merged["first_mismatch_field"] = field
        super().__init__(message, details=merged)
        self.first_mismatch_step = first_mismatch_step
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["first_mismatch_step"] = self.first_mismatch_step
        payload["first_mismatch_field"] = self.field
        return payload


class InputError(DomainError):
    """A local file or path is unavailable or unreadable."""

    code = "E_INPUT"
