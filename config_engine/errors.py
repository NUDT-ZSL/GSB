"""Business errors and stable process exit handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineError(Exception):
    """An expected, diagnosable business error.

    The code is part of the public CLI/API contract.  Details must contain only
    deterministic JSON-compatible values.
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "details": self.details,
            "message": self.message,
            "ok": False,
        }


ERROR_CODES = {
    "CONFIG_PACKAGE_MISSING": "A configured package, shard, database, or event file is missing.",
    "JSON_MALFORMED": "A JSON file is not valid strict JSON.",
    "SCHEMA_INVALID": "A document does not match its declared schema.",
    "DUPLICATE_SERVICE_ENV": "A v1 manifest contains a duplicate service/environment pair.",
    "INVALID_REPLICAS": "Replicas must be a positive integer.",
    "INVALID_TIMEOUT": "Timeout must be a non-negative integer number of milliseconds.",
    "INVALID_RETRY_FACTOR": "Retry factor must be a finite positive number.",
    "INVALID_EFFECTIVE_AFTER": "Relative effective seconds must be a non-negative integer.",
    "INVALID_PARAMS": "Parameters must be a flat JSON map with string keys and scalar values.",
    "PARAM_DECLARATION_MISMATCH": "A reserved parameter contradicts its v1 declaration.",
    "SHARD_HASH_MISMATCH": "A v2 shard hash does not match SQLite metadata.",
    "GENERATION_REGRESSION": "Configuration generation moves backwards.",
    "GENERATION_CONFLICT": "One generation has conflicting immutable content.",
    "INVALID_EFFECTIVE_AT": "Absolute effective time must be an integer millisecond timestamp.",
    "MISSING_UPSTREAM": "A declared upstream service does not exist.",
    "DEPENDENCY_CYCLE": "The service dependency graph contains a cycle.",
    "CLOCK_ROLLBACK": "Injected logical clock moved backwards beyond tolerance.",
    "CLOCK_INVALID": "The injected logical clock is not a non-negative integer.",
    "EVENT_TARGET_UNKNOWN": "An event references an undeclared service/environment.",
    "EVENT_SEQUENCE_INVALID": "An event stream has invalid sequence numbers or duplicate events.",
    "EVENT_SEQUENCE_GAP": "An event stream cannot advance across a missing sequence number.",
    "EVENT_SEQ_CONFLICT": "One event sequence number was reused with different content.",
    "CONTENT_DIGEST_MISMATCH": "An event content digest does not match its candidate payload.",
    "EVENT_CONTENT_NOT_FOUND": "An event references content absent from the config package.",
    "LOWER_GENERATION": "Rejected because it would move the service generation backwards.",
    "LOWER_VERSION": "Rejected because another conflict candidate has a higher version.",
    "LOWER_SOURCE_PRIORITY": "Rejected because another candidate has a higher source priority.",
    "EARLIER_EFFECTIVE_AT": "Rejected because another candidate takes effect later.",
    "DETERMINISTIC_TIEBREAK": "Rejected by the deterministic final tie-break.",
    "LOWER_LOGICAL_CLOCK": "Rejected because another proposal has a newer manifest logical clock.",
    "BASE_CONFIG_MISSING": "reconcile requires a committed base merge first.",
    "OPERATION_CONFLICT": "An operation identifier was reused with different input.",
    "PACKAGE_CHANGED_SINCE_CRASH": "Input files changed after an interrupted operation.",
    "INVALID_EVENT": "The event stream envelope or event fields are invalid.",
    "INVALID_DB": "The SQLite metadata/state database is unreadable or malformed.",
    "VERIFY_FAILED": "Persisted state and the visible view are inconsistent.",
    "INVALID_ARGUMENT": "A command-line argument is invalid.",
}


def error(code: str, message: str | None = None, **details: Any) -> EngineError:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown business error code: {code}")
    return EngineError(code=code, message=message or ERROR_CODES[code], details=details)

