from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NODE_NOT_FOUND = "NODE_NOT_FOUND"
    INVALID_EDGE_LABEL = "INVALID_EDGE_LABEL"
    INVALID_DEPTH = "INVALID_DEPTH"
    SHARD_HASH_MISMATCH = "SHARD_HASH_MISMATCH"
    CLOCK_ROLLBACK_EXCEEDED = "CLOCK_ROLLBACK_EXCEEDED"

    V1_INVALID_JSON = "V1_INVALID_JSON"
    V1_DUPLICATE_NODE_ID = "V1_DUPLICATE_NODE_ID"
    V1_ADJACENCY_MISMATCH = "V1_ADJACENCY_MISMATCH"
    V1_INVALID_WEIGHT = "V1_INVALID_WEIGHT"
    V1_INVALID_WRITTEN_SECONDS = "V1_INVALID_WRITTEN_SECONDS"
    V1_INDEX_MISMATCH = "V1_INDEX_MISMATCH"
    V1_INVALID_RECORD = "V1_INVALID_RECORD"

    V2_INVALID_JSON = "V2_INVALID_JSON"
    V2_MANIFEST_INVALID = "V2_MANIFEST_INVALID"
    V2_METADATA_INVALID = "V2_METADATA_INVALID"
    V2_SHARD_MISMATCH = "V2_SHARD_MISMATCH"
    V2_DUPLICATE_NODE_ID = "V2_DUPLICATE_NODE_ID"
    V2_GENERATION_REGRESSION = "V2_GENERATION_REGRESSION"
    V2_INVALID_TIMESTAMP = "V2_INVALID_TIMESTAMP"
    V2_TOMBSTONE_UNKNOWN_NODE = "V2_TOMBSTONE_UNKNOWN_NODE"
    V2_NODE_DIGEST_MISMATCH = "V2_NODE_DIGEST_MISMATCH"

    SNAPSHOT_CONFLICT = "SNAPSHOT_CONFLICT"
    VERSION_ROLLBACK = "VERSION_ROLLBACK"
    INDEX_NOT_FOUND = "INDEX_NOT_FOUND"
    INDEX_VERIFY_FAILED = "INDEX_VERIFY_FAILED"
    CONTENT_DIGEST_MISMATCH = "CONTENT_DIGEST_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    TRAVERSAL_MISMATCH = "TRAVERSAL_MISMATCH"
    BATCH_FAILED = "BATCH_FAILED"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    RUN_NOT_RESUMABLE = "RUN_NOT_RESUMABLE"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"


@dataclass
class GraphStoreError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


def fail(code: str, message: str, **details: Any) -> GraphStoreError:
    return GraphStoreError(code=code, message=message, details=details)

