from __future__ import annotations

from typing import Any


class DomainError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_code": self.code,
            "message": self.message,
            "details": self.details,
        }

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_DUP_DIALOG = "E_DUP_DIALOG"
E_REGISTRY_INVALID = "E_REGISTRY_INVALID"
E_FRAME_COUNT_MISMATCH = "E_FRAME_COUNT_MISMATCH"
E_SPEAKER_SET_INVALID = "E_SPEAKER_SET_INVALID"
E_SEGMENT_GAP = "E_SEGMENT_GAP"
E_INTERVAL_INVALID = "E_INTERVAL_INVALID"
E_INCONSISTENT = "E_INCONSISTENT"
E_CACHE_KEY_MISMATCH = "E_CACHE_KEY_MISMATCH"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_CACHE_BUSY = "E_CACHE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCREMENTAL_DIVERGENCE = "E_INCREMENTAL_DIVERGENCE"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
