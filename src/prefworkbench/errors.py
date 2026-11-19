from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkbenchError(Exception):
    """A structured, user-visible business error."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    node: str | None = None
    version: int | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.node is not None:
            self.details.setdefault("node", self.node)
        if self.version is not None:
            self.details.setdefault("version", self.version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "node": self.node,
            "version": self.version,
            "details": self.details,
        }


E_CONFIG = "E_CONFIG"
E_INPUT = "E_INPUT"
E_PAIR_CONFLICT = "E_PAIR_CONFLICT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_STALE_VERSION = "E_STALE_VERSION"
E_INCONSISTENT = "E_INCONSISTENT"
E_INTERNAL = "E_INTERNAL"
