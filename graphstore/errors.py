from __future__ import annotations

from typing import Any


class GraphStoreError(Exception):
    """A business error with a stable code and diagnostic details."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "details": self.details,
                "message": self.message,
            }
        }


# Request and query errors.
E_NODE_NOT_FOUND = "GS_NODE_NOT_FOUND"
E_INVALID_EDGE_LABEL = "GS_INVALID_EDGE_LABEL"
E_INVALID_DEPTH = "GS_INVALID_DEPTH"
E_INVALID_QUERY = "GS_INVALID_QUERY"
E_BATCH_NOT_FOUND = "GS_BATCH_NOT_FOUND"
E_BATCH_CONFLICT = "GS_BATCH_CONFLICT"
E_BATCH_DIGEST_MISMATCH = "GS_BATCH_DIGEST_MISMATCH"

# Snapshot and package validation errors.
E_INVALID_JSON = "GS_INVALID_JSON"
E_DUPLICATE_JSON_KEY = "GS_DUPLICATE_JSON_KEY"
E_MISSING_FILE = "GS_MISSING_FILE"
E_V1_SCHEMA = "GS_V1_SCHEMA"
E_V1_DUPLICATE_NODE_ID = "GS_V1_DUPLICATE_NODE_ID"
E_V1_ADJACENCY_MISMATCH = "GS_V1_ADJACENCY_MISMATCH"
E_V1_INVALID_WEIGHT = "GS_V1_INVALID_WEIGHT"
E_V1_INVALID_RELATIVE_TIME = "GS_V1_INVALID_RELATIVE_TIME"
E_V1_LINEAR_INDEX = "GS_V1_LINEAR_INDEX"
E_V2_SCHEMA = "GS_V2_SCHEMA"
E_V2_METADATA = "GS_V2_METADATA"
E_V2_DUPLICATE_CANONICAL_ID = "GS_V2_DUPLICATE_CANONICAL_ID"
E_V2_EDGE_INVALID = "GS_V2_EDGE_INVALID"
E_V2_GENERATION = "GS_V2_GENERATION"
E_V2_TOMBSTONE = "GS_V2_TOMBSTONE"
E_SHARD_HASH_MISMATCH = "GS_SHARD_HASH_MISMATCH"
E_SOURCE_HASH_MISMATCH = "GS_SOURCE_HASH_MISMATCH"
E_PACKAGE_SCHEMA = "GS_PACKAGE_SCHEMA"
E_PACKAGE_NODE_BOUNDS = "GS_PACKAGE_NODE_BOUNDS"
E_PACKAGE_ADJACENCY_DIGEST = "GS_PACKAGE_ADJACENCY_DIGEST"

# Version, clock and state errors.
E_VERSION_ROLLBACK = "GS_VERSION_ROLLBACK"
E_VERSION_CONFLICT = "GS_VERSION_CONFLICT"
E_CLOCK_ROLLBACK = "GS_CLOCK_ROLLBACK"
E_INDEX_NOT_FOUND = "GS_INDEX_NOT_FOUND"
E_BUILD_IN_PROGRESS = "GS_BUILD_IN_PROGRESS"
E_STATE_CORRUPT = "GS_STATE_CORRUPT"
E_LOCK_TIMEOUT = "GS_LOCK_TIMEOUT"


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise GraphStoreError(code, message, **details)

