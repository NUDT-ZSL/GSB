"""Business error codes and the single exception type used across the app.

Every public failure is represented by :class:`WorkbenchError` carrying a
stable string code. ``to_json_dict`` produces the canonical JSON error
object used by the CLI, the GUI and the tests::

    {"ok": false, "code": "E_SEL_RANGE", "message": "...", "detail": {...}}
"""

from __future__ import annotations

from typing import Any

E_SEL_RANGE = "E_SEL_RANGE"
E_NODE_MISSING = "E_NODE_MISSING"
E_CACHE_HASH = "E_CACHE_HASH"
E_VERSION_CONFLICT = "E_VERSION_CONFLICT"
E_PARAM = "E_PARAM"
E_STATE = "E_STATE"
E_IO = "E_IO"
E_UNKNOWN = "E_UNKNOWN"

ERROR_CODES = (
    E_SEL_RANGE,
    E_NODE_MISSING,
    E_CACHE_HASH,
    E_VERSION_CONFLICT,
    E_PARAM,
    E_STATE,
    E_IO,
    E_UNKNOWN,
)


class WorkbenchError(Exception):
    """Raised for every expected business failure.

    The caller can always serialize it to a JSON-serializable dict; the
    workspace is left untouched (no partial writes) whenever this escapes a
    mutating call.
    """

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        if code not in ERROR_CODES:
            code = E_UNKNOWN
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"
