"""Engine exception types."""

from __future__ import annotations


class EngineError(Exception):
    """An expected engine failure with a stable business code."""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class RejectRecord(EngineError):
    """A non-fatal record rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        source: str,
        record_index: int,
        field: str | None = None,
        details: dict | None = None,
    ):
        super().__init__(code, message, details=details)
        self.source = source
        self.record_index = record_index
        self.field = field

    def as_dict(self) -> dict:
        result = {
            "source": self.source,
            "record_index": self.record_index,
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            result["field"] = self.field
        if self.details:
            result["details"] = self.details
        return result


class HostTimeout(Exception):
    """Internal control flow used to stop one host task."""
