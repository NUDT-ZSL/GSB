from __future__ import annotations

from typing import Any


CODES = {
    "OK": "ok",
    "E_MASK_DEGENERATE": "degenerate polygon or repeated mask edge",
    "E_MASK_INTERSECT": "mask polygon self-intersects",
    "E_MASK_OUT_OF_BOUNDS": "mask coordinate is outside image canvas",
    "E_STYLE_INVALID": "style_strength is outside [0, 1]",
    "E_TRANSFORM_INVALID": "colour migration matrix is invalid",
    "E_BLOCK_MISMATCH": "block path differs from one-shot reference",
    "E_MEMORY_BUDGET": "estimated resident memory exceeds budget",
    "E_BLOCK_CONCURRENCY": "requested block concurrency exceeds limit",
    "E_STALE_RESULT": "result was produced from an outdated cache key",
    "E_CHECKPOINT_CORRUPT": "checkpoint checksum or version chain is invalid",
    "E_SCHEMA_INVALID": "task or parameter schema is invalid",
}


class WorkbenchError(Exception):
    """Structured error carrying one of the public business codes."""

    def __init__(self, code: str, message: str | None = None, **details: Any):
        if code not in CODES:
            raise ValueError(f"unknown business code {code}")
        self.code = code
        self.details = details
        super().__init__(message or CODES[code])

    def to_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "message": str(self)}
        payload.update(self.details)
        return payload


def error_record(code: str, edit_id: str | None = None, **details: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"ok": False, "code": code}
    if edit_id is not None:
        record["edit_id"] = edit_id
    record.update(details)
    return record
