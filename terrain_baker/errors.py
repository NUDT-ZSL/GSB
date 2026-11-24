from __future__ import annotations

from typing import Any, Dict, Optional


class BakerError(Exception):
    """Base class for expected engine failures."""

    code = "E_BAKE"
    exit_code = 1

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


class SchemaError(BakerError):
    code = "E_SCHEMA"
    exit_code = 2


class HeightFieldError(BakerError):
    code = "E_HEIGHTFIELD"
    exit_code = 2


class RenderTaskError(BakerError):
    code = "E_RENDER"
    exit_code = 3


class MemoryBudgetError(BakerError):
    code = "E_MEMORY_BUDGET"
    exit_code = 4


class BakeInterrupted(BakerError):
    code = "E_INTERRUPTED"
    exit_code = 130


def schema_error(path: str, message: str, **details: Any) -> SchemaError:
    payload = {"path": path}
    payload.update(details)
    return SchemaError(message, payload)
