"""Structured business errors for the deterministic dedup workbench."""
from __future__ import annotations

from typing import Any


class WorkbenchError(Exception):
    """An error with a stable machine-readable business code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            error["field"] = self.field
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


E_JSONL_PARSE = "E_JSONL_PARSE"
E_INPUT_RECORD = "E_INPUT_RECORD"
E_DUPLICATE_DOC_ID = "E_DUPLICATE_DOC_ID"
E_EMPTY_TEXT = "E_EMPTY_TEXT"
E_CONTENT_HASH_MISMATCH = "E_CONTENT_HASH_MISMATCH"
E_CONTENT_HASH_FORMAT = "E_CONTENT_HASH_FORMAT"
E_LANG_NOT_ALLOWED = "E_LANG_NOT_ALLOWED"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CHECKSUM = "E_SNAPSHOT_CHECKSUM"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_INCONSISTENT = "E_INCONSISTENT"
E_CHECKPOINT = "E_CHECKPOINT"
E_NOT_FOUND = "E_NOT_FOUND"
