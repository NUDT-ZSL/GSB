"""Business errors with stable codes and diagnostic fields."""

from __future__ import annotations

from typing import Any


class AlarmError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "details": self.details,
        }


def fail(code: str, message: str, **details: Any) -> None:
    raise AlarmError(code, message, **details)


ERROR_CODES = {
    "INPUT_JSON_INVALID": "JSON cannot be parsed or contains duplicate keys/non-finite constants.",
    "INPUT_SHAPE_INVALID": "Input does not have the expected object/list shape.",
    "TOPOLOGY_VERSION_INVALID": "Topology version is missing or is not a positive integer.",
    "TOPOLOGY_DUPLICATE_SERVICE": "A service name occurs more than once.",
    "TOPOLOGY_DUPLICATE_INSTANCE": "An instance ID occurs more than once.",
    "TOPOLOGY_UNKNOWN_DEPENDENCY": "A dependency references an undeclared service.",
    "TOPOLOGY_SELF_DEPENDENCY": "A service directly depends on itself.",
    "TOPOLOGY_INSTANCE_UNKNOWN_SERVICE": "An instance references an undeclared service.",
    "TOPOLOGY_CYCLE": "The dependency graph contains a directed cycle.",
    "TOPOLOGY_WEIGHT_INVALID": "An instance weight is not finite or is not greater than zero.",
    "EVENT_SHAPE_INVALID": "An alert event has an invalid field type or shape.",
    "EVENT_SEQUENCE_NOT_STRICT": "Event sequence numbers are not strictly increasing in input order.",
    "EVENT_SEQUENCE_CONFLICT": "An event sequence already exists with different content.",
    "EVENT_NEGATIVE_TS": "An event logical timestamp is negative.",
    "EVENT_UNKNOWN_INSTANCE": "An event references an undeclared instance.",
    "EVENT_INVALID_SEVERITY": "An event severity is outside the allowed enum.",
    "EVENT_INVALID_FLAG": "A silence/maintenance flag is not boolean.",
    "EVENT_METRIC_NOT_FINITE": "An event metric value is not finite.",
    "PATCH_SHAPE_INVALID": "A patch has an invalid shape.",
    "PATCH_ID_CONFLICT": "A patch ID was previously used with different content.",
    "PATCH_VERSION_CONFLICT": "A topology version is already occupied by different content.",
    "PATCH_BASE_MISMATCH": "Patch base version does not match the current topology version.",
    "PATCH_BASE_VERSION_AHEAD": "Patch base version is ahead of the current topology version.",
    "PATCH_VERSION_INVALID": "Patch target version is not greater than its base version.",
    "PATCH_TOPOLOGY_VERSION_ROLLBACK": "Patch target version would roll topology state backwards.",
    "PATCH_UNKNOWN_SERVICE": "A patch references an undeclared service.",
    "PATCH_UNKNOWN_INSTANCE": "A patch references an undeclared instance.",
    "PATCH_UNKNOWN_WINDOW": "A patch references an undeclared silence window.",
    "PATCH_INSTANCE_SERVICE_MISMATCH": "A patched instance does not belong to the declared service.",
    "PATCH_INVALID_SILENCE_WINDOW": "A silence/maintenance window interval or scope is invalid.",
    "PATCH_UNSUPPORTED_CHANGE": "A patch change type/action is unsupported.",
    "STATE_NOT_INITIALIZED": "The SQLite state has not been initialized by load.",
    "STATE_INCONSISTENT": "Persisted state failed an internal consistency check.",
    "STATE_TOPOLOGY_LOAD_CONFLICT": "An existing database contains a different initial topology.",
    "CLUSTER_WINDOW_MISMATCH": "The requested cluster window differs from the persisted window.",
    "JOB_FAILED": "A persisted operation failed during replay.",
    "DB_BUSY": "The SQLite database could not be locked within the configured timeout.",
    "INTERNAL_ERROR": "An unexpected internal error occurred.",
}
