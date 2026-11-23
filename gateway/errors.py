"""Business errors used across the gateway."""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """An expected, machine-readable business error.

    The code is stable and deliberately independent from Python exception
    classes.  Diagnostics are JSON serialisable primitives.
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {key: value for key, value in details.items() if value is not None}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        payload.update(self.details)
        return payload
