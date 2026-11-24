"""Business error codes and the single exception type used by the engine.

Every validation or runtime failure that callers are expected to handle is an
:class:`EngineError` carrying a stable ``E_xxx`` code.  The ``to_dict`` helper
produces the JSON error envelope emitted by the CLI; on validation failure no
output file is ever created before the error is raised.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# Stable business codes (kept as constants so tests can import them).
E_PARSE = "E_PARSE"
E_SCHEMA = "E_SCHEMA"
E_GRID_DIMS = "E_GRID_DIMS"
E_SCALE = "E_SCALE"
E_HEIGHT_COUNT = "E_HEIGHT_COUNT"
E_TILE_COVERAGE = "E_TILE_COVERAGE"
E_TILE_DIVISIBILITY = "E_TILE_DIVISIBILITY"
E_SAMPLES = "E_SAMPLES"
E_CAMERA_AXIS = "E_CAMERA_AXIS"
E_LIGHT = "E_LIGHT"
E_DIGEST = "E_DIGEST"
E_BOUNDS = "E_BOUNDS"
E_BUDGET = "E_BUDGET"
E_STATE = "E_STATE"
E_IO = "E_IO"


class EngineError(Exception):
    """Raised for all expected engine failures with a stable business code."""

    def __init__(
            self,
            code: str,
            message: str,
            details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details: Dict[str, Any] = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def fail(code: str, message: str, **details: Any) -> EngineError:
    """Build an :class:`EngineError` with keyword-only detail fields."""
    return EngineError(code, message, details)

