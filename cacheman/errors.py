"""Business errors shared by the parsers, engine, and CLI."""

from __future__ import annotations

from typing import Any


CACHE_MANIFEST_INVALID = "CACHE_MANIFEST_INVALID"
CACHE_ARTIFACT_MISSING = "CACHE_ARTIFACT_MISSING"
CACHE_DIGEST_MISMATCH = "CACHE_DIGEST_MISMATCH"
CACHE_SHARD_HASH_MISMATCH = "CACHE_SHARD_HASH_MISMATCH"
CACHE_TOMBSTONE_INVALID = "CACHE_TOMBSTONE_INVALID"
CACHE_GENERATION_REGRESSION = "CACHE_GENERATION_REGRESSION"
CACHE_MANIFEST_VERSION_ROLLBACK = "CACHE_MANIFEST_VERSION_ROLLBACK"
CACHE_CLOCK_ROLLBACK = "CACHE_CLOCK_ROLLBACK"
CACHE_PIPELINE_INVALID = "CACHE_PIPELINE_INVALID"
CACHE_STATE_CORRUPT = "CACHE_STATE_CORRUPT"
CACHE_LOCK_TIMEOUT = "CACHE_LOCK_TIMEOUT"
CACHE_INDEX_EMPTY = "CACHE_INDEX_EMPTY"
CACHE_INTERNAL_ERROR = "CACHE_INTERNAL_ERROR"


class CacheError(Exception):
    """An error with a stable machine-readable business code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fields: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields or {}
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "fields": self.fields,
            },
        }
