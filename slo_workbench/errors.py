"""Business error codes and exception types used across the workbench.

Every user-facing failure is mapped to a stable ``E_*`` code so that the CLI,
HTTP layer and tests can assert on a deterministic diagnostic contract.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class BusinessError(Exception):
    """An error carrying a stable code plus structured diagnostic fields."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields: Dict[str, Any] = dict(fields or {})

    def to_dict(self) -> Dict[str, Any]:
        payload = {"code": self.code, "message": self.message}
        if self.fields:
            payload["fields"] = self.fields
        return payload


E_BAD_SLO = "E_BAD_SLO"
E_BAD_EVENT = "E_BAD_EVENT"
E_EMPTY_WINDOW = "E_EMPTY_WINDOW"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
E_SAMPLE_MISMATCH = "E_SAMPLE_MISMATCH"
E_CACHE_MISMATCH = "E_CACHE_MISMATCH"
E_NOT_FOUND = "E_NOT_FOUND"
E_BAD_INPUT = "E_BAD_INPUT"
E_STATE = "E_STATE"
E_LOCK = "E_LOCK"


def bad_slo(message: str, **fields: Any) -> BusinessError:
    return BusinessError(E_BAD_SLO, message, fields=fields)


def bad_event(message: str, **fields: Any) -> BusinessError:
    return BusinessError(E_BAD_EVENT, message, fields=fields)
