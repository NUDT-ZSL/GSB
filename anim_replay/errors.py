from __future__ import annotations

from typing import Any, Mapping


class AnimError(Exception):
    """Base class for structured, user-facing failures."""

    code = "E_ANIM"

    def __init__(
        self,
        message: str,
        *,
        location: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.location = location
        self.details = dict(details or {})

    def as_error(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.location is not None:
            result["location"] = self.location
        if self.details:
            result["details"] = self.details
        return result

    def as_payload(self) -> dict[str, Any]:
        return {"ok": False, "error": self.as_error()}


class SchemaError(AnimError):
    code = "E_SCHEMA"


class DigestError(AnimError):
    code = "E_DIGEST"


class ReplayError(AnimError):
    code = "E_REPLAY"


class VersionError(AnimError):
    code = "E_VERSION"


class BudgetError(AnimError):
    code = "E_BUDGET"


def schema_error(message: str, location: str | None = None, **details: Any) -> SchemaError:
    return SchemaError(message, location=location, details=details)

