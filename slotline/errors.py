from __future__ import annotations

from typing import Any


E_PENDING_SLOT_MISSING = "E_PENDING_SLOT_MISSING"
E_THRESHOLD_CONFLICT = "E_THRESHOLD_CONFLICT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_OPERATION_ORDER = "E_OPERATION_ORDER"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_LINEAGE_STALE = "E_LINEAGE_STALE"
E_TURN_NOT_FOUND = "E_TURN_NOT_FOUND"
E_TURN_ALREADY_RETRACTED = "E_TURN_ALREADY_RETRACTED"
E_INVALID_SESSION = "E_INVALID_SESSION"
E_INVALID_CONFIG = "E_INVALID_CONFIG"


class BusinessError(Exception):
    """A deliberately serializable business failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        slot: str | None = None,
        node: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.slot = slot
        self.node = node
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "slot": self.slot,
            "slot_key": self.slot,
            "node": self.node,
            "details": self.details,
        }

    def with_details(self, **details: Any) -> "BusinessError":
        merged = dict(self.details)
        merged.update(details)
        return BusinessError(
            self.code,
            self.message,
            slot=self.slot,
            node=self.node,
            details=merged,
        )
