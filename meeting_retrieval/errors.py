"""Structured business errors used by the CLI, service, and tests."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, Optional


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    pair_id: Optional[str] = None
    field: Optional[str] = None
    details: Dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.pair_id is not None:
            error["pair_id"] = self.pair_id
        if self.field is not None:
            error["field"] = self.field
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


E_DUPLICATE_PAIR_ID = "E_DUPLICATE_PAIR_ID"
E_IMAGE_SHAPE_MISMATCH = "E_IMAGE_SHAPE_MISMATCH"
E_IMAGE_SEED_INVALID = "E_IMAGE_SEED_INVALID"
E_TEXT_EMPTY = "E_TEXT_EMPTY"
E_GRID_INVALID = "E_GRID_INVALID"
E_RECT_INVALID = "E_RECT_INVALID"
E_LOCALE_INVALID = "E_LOCALE_INVALID"
E_VOCAB_MISSING = "E_VOCAB_MISSING"
E_VOCAB_OUT_OF_RANGE = "E_VOCAB_OUT_OF_RANGE"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CONFLICT = "E_SNAPSHOT_CONFLICT"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_CACHE_MISS = "E_CACHE_MISS"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_HIT_NOT_FOUND = "E_HIT_NOT_FOUND"
E_INPUT_INVALID = "E_INPUT_INVALID"
E_STATE_INVALID = "E_STATE_INVALID"


def require(condition: bool, code: str, message: str, **kwargs: Any) -> None:
    if not condition:
        raise BusinessError(code=code, message=message, **kwargs)
