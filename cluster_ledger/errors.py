"""Business error types shared by the CLI, services, ledger, and UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_CONFIG = "E_CONFIG"
E_PORT = "E_PORT"
E_VERSION = "E_VERSION"
E_LEDGER = "E_LEDGER"
E_VERIFY = "E_VERIFY"
E_LATENCY = "E_LATENCY"
E_REPLAY = "E_REPLAY"
E_SERVICE = "E_SERVICE"


@dataclass
class BusinessError(Exception):
    """An expected, machine-readable application failure.

    ``field_path`` uses dotted/indexed JSON paths such as
    ``clusters[1].port`` and is present for configuration failures.
    """

    code: str
    message: str
    field_path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.field_path is not None:
            payload["field_path"] = self.field_path
        if self.details:
            payload["details"] = self.details
        return payload


def require(condition: bool, code: str, message: str, field_path: str | None = None,
            **details: Any) -> None:
    if not condition:
        raise BusinessError(code, message, field_path=field_path, details=details)

