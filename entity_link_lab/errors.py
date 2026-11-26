"""Stable business error codes for the deterministic entity-link lab."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_STALE_LINK = "E_STALE_LINK"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_INCONSISTENT = "E_INCONSISTENT"
E_CHECKPOINT_DIRTY = "E_CHECKPOINT_DIRTY"
E_DUPLICATE_CHAIN = "E_DUPLICATE_CHAIN"
E_INPUT_INVALID = "E_INPUT_INVALID"
E_RECOVERY_FAILED = "E_RECOVERY_FAILED"


@dataclass
class LinkError(Exception):
    """A machine-readable failure with a stable code and structured details."""

    code: str
    message: str
    stage: str = "entity_link_lab"
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "status": "error",
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload
