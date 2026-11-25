"""Business error model used by the routing pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


E_INVALID_INPUT = "E_INVALID_INPUT"
E_DUPLICATE_CASE_ID = "E_DUPLICATE_CASE_ID"
E_GATE_DIMENSION_MISMATCH = "E_GATE_DIMENSION_MISMATCH"
E_TOKEN_DIMENSION_MISMATCH = "E_TOKEN_DIMENSION_MISMATCH"
E_EXPERT_DIMENSION_MISMATCH = "E_EXPERT_DIMENSION_MISMATCH"
E_EXPERT_COUNT_MISMATCH = "E_EXPERT_COUNT_MISMATCH"
E_INVALID_BLOCK_SIZE = "E_INVALID_BLOCK_SIZE"
E_REVISION_REGRESSION = "E_REVISION_REGRESSION"
E_BLOCK_NOT_READY = "E_BLOCK_NOT_READY"
E_BLOCK_MISMATCH = "E_BLOCK_MISMATCH"
E_STALE_RESULT = "E_STALE_RESULT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"


@dataclass
class MoeError(Exception):
    """A structured, serializable business error.

    The ``code`` is stable for integration callers. ``field`` identifies the
    offending input or configuration field whenever one exists.
    """

    code: str
    message: str
    case_id: str | None = None
    field: str | None = None
    block_index: int | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.case_id is not None:
            payload["case_id"] = self.case_id
        if self.field is not None:
            payload["field"] = self.field
        if self.block_index is not None:
            payload["block_index"] = self.block_index
        if self.details:
            payload["details"] = self.details
        return payload

