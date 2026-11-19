"""Business errors with stable machine-readable codes."""

from __future__ import annotations

from typing import Any


class IntentError(Exception):
    """An expected, attributable business error.

    The CLI and the injection framework serialize this object instead of
    leaking tracebacks.  ``details`` intentionally accepts arbitrary context,
    while the commonly queried fields are also exposed as attributes.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.details: dict[str, Any] = details

    @property
    def line_no(self) -> int | None:
        value = self.details.get("line_no")
        return value if isinstance(value, int) else None

    @property
    def query_id(self) -> str | None:
        value = self.details.get("query_id")
        return value if isinstance(value, str) else None

    def to_record(self, *, attributed: bool = True) -> dict[str, Any]:
        record: dict[str, Any] = {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "attributed": attributed,
        }
        for key in (
            "line_no",
            "query_id",
            "field",
            "version",
            "field_offset",
            "category",
            "intent",
            "injection_stage",
            "injection_kind",
        ):
            if key in self.details:
                record[key] = self.details[key]
        record["details"] = self.details
        return record

    def __repr__(self) -> str:
        return (
            f"IntentError(code={self.code!r}, stage={self.stage!r}, "
            f"details={self.details!r})"
        )


def unattributed_record(stage: str, exc: BaseException) -> dict[str, Any]:
    """Convert an unexpected exception into an explicitly unattributed row."""

    return {
        "stage": stage,
        "code": "E_UNATTRIBUTED",
        "message": str(exc),
        "attributed": False,
        "exception_type": type(exc).__name__,
    }
