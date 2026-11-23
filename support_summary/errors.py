"""Structured business errors used by the library and CLI."""

from __future__ import annotations

from typing import Any, Dict, Optional


class BusinessError(Exception):
    """An expected domain failure with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        session_id: Optional[str] = None,
        field: Optional[str] = None,
        stage: Optional[str] = None,
        version: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.session_id = session_id
        self.field = field
        self.stage = stage
        self.version = version
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.session_id is not None:
            value["session_id"] = self.session_id
        if self.field is not None:
            value["field"] = self.field
        if self.stage is not None:
            value["stage"] = self.stage
        if self.version is not None:
            value["version"] = self.version
        if self.details:
            value["details"] = self.details
        return value


class InjectedCrash(RuntimeError):
    """Used by tests and the recovery demo before an uncommitted stage."""
