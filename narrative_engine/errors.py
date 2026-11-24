from __future__ import annotations

from typing import Any


EXIT_SCHEMA = 2
EXIT_DIGEST = 3
EXIT_RESOURCE = 4
EXIT_RUNTIME = 5
EXIT_INTERRUPTED = 130


class NarrativeError(Exception):
    """Base class for JSON-reportable engine errors."""

    code = "E_ENGINE"
    exit_code = EXIT_RUNTIME

    def __init__(
        self,
        message: str,
        *,
        details: Any | None = None,
        code: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code is not None:
            self.code = code
        if exit_code is not None:
            self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"ok": False, "error": error}


class SchemaError(NarrativeError):
    code = "E_SCHEMA"
    exit_code = EXIT_SCHEMA


class DigestError(NarrativeError):
    code = "E_DIGEST"
    exit_code = EXIT_DIGEST


class ResourceLimitError(NarrativeError):
    code = "E_RESOURCE"
    exit_code = EXIT_RESOURCE


class ChoiceError(NarrativeError):
    code = "E_SCHEMA"
    exit_code = EXIT_SCHEMA


class Interrupted(NarrativeError):
    code = "E_INTERRUPTED"
    exit_code = EXIT_INTERRUPTED

    def __init__(self, message: str = "run interrupted at event boundary", *, exit_code: int = EXIT_INTERRUPTED):
        super().__init__(message, exit_code=exit_code)
        self.exit_code = exit_code
