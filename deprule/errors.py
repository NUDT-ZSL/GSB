"""Business errors and stable result codes."""

from __future__ import annotations


class Codes:
    OK = "OK"
    E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
    E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
    E_STALE_RESULT = "E_STALE_RESULT"
    E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    E_STAGE_BUSY = "E_STAGE_BUSY"
    E_TOKEN_VALIDATION = "E_TOKEN_VALIDATION"
    E_NOT_FOUND = "E_NOT_FOUND"
    E_CONFLICT = "E_CONFLICT"
    E_INTERNAL = "E_INTERNAL"


class DomainError(Exception):
    """An expected error that can be rendered as a business result."""

    code = Codes.E_INTERNAL

    def __init__(self, message: str, *, code: str | None = None, **details):
        super().__init__(message)
        if code:
            self.code = code
        self.message = message
        self.details = details

    def to_envelope(self) -> dict:
        body = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            body["details"] = _jsonable(self.details)
        return body


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
