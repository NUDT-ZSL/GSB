"""Business error codes and the domain exception type."""

from __future__ import annotations

from typing import Any, Dict, Optional


OK = "OK"
E_CONFIDENCE_CONFLICT = "E_CONFIDENCE_CONFLICT"
E_INCREMENTAL_MISMATCH = "E_INCREMENTAL_MISMATCH"
E_LINEAGE_STALE = "E_LINEAGE_STALE"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_INCONSISTENT = "E_INCONSISTENT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_INVALID_INPUT = "E_INVALID_INPUT"


BUSINESS_CODES = {
    OK,
    E_CONFIDENCE_CONFLICT,
    E_INCREMENTAL_MISMATCH,
    E_LINEAGE_STALE,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_ROLLBACK,
    E_SNAPSHOT_CORRUPT,
    E_INCONSISTENT,
    E_CACHE_CORRUPT,
    E_INVALID_INPUT,
}


class BusinessError(Exception):
    """A structured business failure that can be serialized into a report."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        image_id: Optional[str] = None,
        snapshot_name: Optional[str] = None,
        category: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        if code not in BUSINESS_CODES:
            raise ValueError("unknown business code: {0}".format(code))
        self.code = code
        self.message = message
        self.image_id = image_id
        self.snapshot_name = snapshot_name
        self.category = category
        self.details = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "business_code": self.code,
            "message": self.message,
        }
        if self.image_id is not None:
            result["image_id"] = self.image_id
        if self.snapshot_name is not None:
            result["snapshot_name"] = self.snapshot_name
        if self.category is not None:
            result["category"] = self.category
        if self.details:
            result["details"] = self.details
        return result
