"""Structured business errors used across the application."""

from __future__ import annotations

from typing import Any


class ErrorCode:
    DUPLICATE_REGION = "E_DUPLICATE_REGION"
    EMPTY_MASK = "E_EMPTY_MASK"
    REGION_OUT_OF_BOUNDS = "E_REGION_OUT_OF_BOUNDS"
    MASK_OVERLAP = "E_MASK_OVERLAP"
    INVALID_REGION_ID = "E_INVALID_REGION_ID"
    INVALID_BASE = "E_INVALID_BASE"

    STYLE_STRENGTH_RANGE = "E_STYLE_STRENGTH_RANGE"
    UNKNOWN_PRECISION = "E_UNKNOWN_PRECISION"
    INVALID_ITERATIONS = "E_INVALID_ITERATIONS"
    INVALID_CONTEXT_RADIUS = "E_INVALID_CONTEXT_RADIUS"
    INVALID_COLOR_MATRIX = "E_INVALID_COLOR_MATRIX"
    INVALID_CONFIG = "E_INVALID_CONFIG"
    INVALID_GRAPH = "E_INVALID_GRAPH"

    STAGE_BUSY = "E_STAGE_BUSY"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    REGION_NODE_FAILURE = "E_REGION_NODE_FAILURE"

    BLEND_MISMATCH = "E_BLEND_MISMATCH"
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    CACHE_CORRUPT = "E_CACHE_CORRUPT"

    INCONSISTENT = "E_INCONSISTENT"
    OUTPUT_MISMATCH = "E_OUTPUT_MISMATCH"
    NO_ACTIVE_STATE = "E_NO_ACTIVE_STATE"
    INTERNAL_ERROR = "E_INTERNAL_ERROR"


class BusinessError(Exception):
    """An error carrying the business code and machine-readable details."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": _json_safe(self.details),
        }

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)
