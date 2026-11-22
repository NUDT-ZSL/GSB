from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    INVALID_JSON = "E1001_INVALID_JSON"
    SCHEMA_INVALID = "E1002_SCHEMA_INVALID"
    PATH_INVALID = "E1003_PATH_INVALID"
    PATH_TRAVERSAL = "E1004_PATH_TRAVERSAL"
    VERSION_INVALID = "E1005_VERSION_INVALID"
    VERSION_NOT_MONOTONIC = "E1006_VERSION_NOT_MONOTONIC"
    ETAG_INVALID = "E1007_ETAG_INVALID"
    ETAG_DIGEST_MISMATCH = "E1008_ETAG_DIGEST_MISMATCH"
    CACHE_CONTROL_INVALID = "E1009_CACHE_CONTROL_INVALID"
    NODE_CONFIG_INVALID = "E1010_NODE_CONFIG_INVALID"
    INJECTION_INVALID = "E1011_INJECTION_INVALID"
    INJECTION_REFERENCE_MISSING = "E1012_INJECTION_REFERENCE_MISSING"
    SHARD_HASH_MISMATCH = "E1013_SHARD_HASH_MISMATCH"
    SHARD_METADATA_INVALID = "E1014_SHARD_METADATA_INVALID"
    CACHE_GENERATION_ROLLBACK = "E1015_CACHE_GENERATION_ROLLBACK"
    DUPLICATE_KEY = "E1016_DUPLICATE_KEY"
    POLICY_INVALID = "E1017_POLICY_INVALID"
    CLOCK_ROLLBACK = "E1101_CLOCK_ROLLBACK"
    ORIGIN_STALE_304 = "E1201_ORIGIN_STALE_304"
    ORIGIN_UNCHANGED_200 = "E1202_ORIGIN_UNCHANGED_200"
    ORIGIN_PARTIAL_CHANGE = "E1203_ORIGIN_PARTIAL_CHANGE"
    ORIGIN_REQUEST_TIMEOUT = "E1204_ORIGIN_REQUEST_TIMEOUT"
    ORIGIN_MALFORMED_HEADERS = "E1205_ORIGIN_MALFORMED_HEADERS"
    ORIGIN_CONNECTION_REFUSED = "E1206_ORIGIN_CONNECTION_REFUSED"
    ORIGIN_RETRY_EXHAUSTED = "E1207_ORIGIN_RETRY_EXHAUSTED"
    ORIGIN_BAD_RESPONSE = "E1208_ORIGIN_BAD_RESPONSE"
    ORIGIN_UNCACHEABLE = "E1209_ORIGIN_UNCACHEABLE"
    INJECTED_CONTENT_TAMPER = "E1301_INJECTED_CONTENT_TAMPER"
    INJECTED_ETAG_CONFLICT = "E1302_INJECTED_ETAG_CONFLICT"
    INJECTED_CLOCK_ROLLBACK = "E1303_INJECTED_CLOCK_ROLLBACK"
    INDEX_VERSION_ROLLBACK = "E1401_INDEX_VERSION_ROLLBACK"
    CONCURRENT_MODIFICATION = "E1402_CONCURRENT_MODIFICATION"
    RECOVERY_INCOMPLETE = "E1403_RECOVERY_INCOMPLETE"
    STATE_CORRUPT = "E1404_STATE_CORRUPT"
    RUN_ID_CONFLICT = "E1405_RUN_ID_CONFLICT"
    INVALID_ARGUMENT = "E1406_INVALID_ARGUMENT"


class BusinessError(Exception):
    """A deterministic, machine-readable workbench failure."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        stage: str = "validation",
        path: str | None = None,
        node: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.path = path
        self.node = node
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "stage": self.stage,
                "path": self.path,
                "node": self.node,
                "details": self.details,
            },
        }


class SimulatedCrash(RuntimeError):
    """Raised by a requested fault point to emulate an abrupt process exit."""


def as_business_error(
    code: ErrorCode,
    message: str,
    *,
    stage: str,
    path: str | None = None,
    node: str | None = None,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(
        code,
        message,
        stage=stage,
        path=path,
        node=node,
        details=details,
    )

