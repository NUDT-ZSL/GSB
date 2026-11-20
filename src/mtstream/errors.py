"""Business errors shared by the library, CLI and HTTP workbench."""

from __future__ import annotations

from typing import Any

E_DUP_CASE = "E_DUP_CASE"
E_TOKEN_UNKNOWN = "E_TOKEN_UNKNOWN"
E_ALIGN_INVALID = "E_ALIGN_INVALID"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CHUNK_MISMATCH = "E_CHUNK_MISMATCH"
E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_INCONSISTENT = "E_INCONSISTENT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SELF_CHECK = "E_SELF_CHECK"


EXIT_CODES = {
    E_CONFIG_INVALID: 2,
    E_TOKEN_UNKNOWN: 2,
    E_ALIGN_INVALID: 2,
    E_DUP_CASE: 2,
    E_CHUNK_MISMATCH: 3,
    E_INCONSISTENT: 3,
    E_SELF_CHECK: 3,
    E_LATENCY_BUDGET: 4,
    E_RESOURCE_LIMIT: 5,
    E_STAGE_BUSY: 6,
    E_CACHE_CORRUPT: 7,
    E_SNAPSHOT_ROLLBACK: 8,
}


class MTStreamError(Exception):
    """An expected, machine-readable business failure.

    ``exit_code`` is deliberately an ``int`` on every platform.  The business
    code is carried in the JSON payload/stderr text instead of being encoded in
    the process status alone.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.data = data or {}
        self.exit_code = int(EXIT_CODES.get(code, 1))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "exit_code": self.exit_code,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.data:
            payload["data"] = self.data
        return payload

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": self.to_dict()}


def fail(
    code: str,
    message: str,
    *,
    field: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    raise MTStreamError(code, message, field=field, data=data)

