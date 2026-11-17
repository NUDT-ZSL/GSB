"""Business error types with stable, machine-readable codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_TXN = "E_TXN"
E_VERSION = "E_VERSION"
E_CHAIN = "E_CHAIN"
E_CACHE = "E_CACHE"
E_VISIBLE = "E_VISIBLE"
E_REPLAY = "E_REPLAY"
E_SNAPSHOT = "E_SNAPSHOT"
E_INPUT = "E_INPUT"
E_IO = "E_IO"


@dataclass
class BusinessError(Exception):
    """An expected domain failure.

    ``details`` is deliberately JSON-serialisable so CLI and GUI callers can
    show the exact chain position, transaction id, or divergence step.
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
