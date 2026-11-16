"""Business error types used by the transaction laboratory."""

from __future__ import annotations

from typing import Any


E_SCRIPT = "E_SCRIPT"
E_LOCKTIMEOUT = "E_LOCKTIMEOUT"
E_DEADLOCK = "E_DEADLOCK"
E_ISOLATION = "E_ISOLATION"
E_REPLAY = "E_REPLAY"
E_STORAGE = "E_STORAGE"

BUSINESS_CODES = (
    E_SCRIPT,
    E_LOCKTIMEOUT,
    E_DEADLOCK,
    E_ISOLATION,
    E_REPLAY,
    E_STORAGE,
)


class LabError(Exception):
    """A failure that has a stable, user-facing business code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        session_id: str | None = None,
        op_index: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.session_id = session_id
        self.op_index = op_index
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.session_id is not None:
            result["session_id"] = self.session_id
        if self.op_index is not None:
            result["op_index"] = self.op_index
        if self.details:
            result["details"] = self.details
        return result


class ScriptError(LabError):
    def __init__(
        self,
        message: str,
        *,
        session_id: str | None = None,
        op_index: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            E_SCRIPT,
            message,
            session_id=session_id,
            op_index=op_index,
            details=details,
        )


class ReplayError(LabError):
    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(E_REPLAY, message, details=details)

