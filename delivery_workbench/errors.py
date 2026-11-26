from __future__ import annotations

from typing import Any, Dict, Optional


class Code:
    OK = "WB_OK"
    VALIDATION = "WB_VALIDATION_FAILED"
    JSON_INVALID = "WB_JSON_INVALID"
    INPUT_MISSING = "WB_INPUT_MISSING"
    TOPIC_DUPLICATE = "WB_TOPIC_NAME_DUPLICATE"
    TOPIC_FIELD = "WB_TOPIC_FIELD_INVALID"
    KEY_RULE_INVALID = "WB_KEY_RULE_INVALID"
    EVENT_KEY_UNCOVERED = "WB_EVENT_KEY_UNCOVERED"
    EVENT_SEQ = "WB_EVENT_SEQUENCE_NOT_STRICT"
    EVENT_FIELD = "WB_EVENT_FIELD_INVALID"
    SUBSCRIBER_DUPLICATE = "WB_SUBSCRIBER_ID_DUPLICATE"
    SUBSCRIPTION_FIELD = "WB_SUBSCRIPTION_FIELD_INVALID"
    CALLBACK_ENDPOINT = "WB_CALLBACK_ENDPOINT_INVALID"
    V1_TIME = "WB_V1_RELATIVE_SECONDS_INVALID"
    SNAPSHOT_FIELD = "WB_SNAPSHOT_FIELD_INVALID"
    V2_HASH = "WB_V2_SHARD_HASH_MISMATCH"
    V2_MONOTONIC = "WB_V2_GENERATION_NOT_MONOTONIC"
    V2_METADATA = "WB_V2_METADATA_MISMATCH"
    SEMANTIC_KEY_UNKNOWN = "WB_SEMANTIC_KEY_UNKNOWN"
    DEPENDENCY_INVALID = "WB_DEPENDENCY_INVALID"
    DEPENDENCY_CYCLE = "WB_DEPENDENCY_CYCLE"
    VERSION_ROLLBACK = "WB_SNAPSHOT_VERSION_ROLLBACK"
    VERSION_CONFLICT = "WB_SNAPSHOT_VERSION_HASH_CONFLICT"
    GENERATION_FLOOR = "WB_GENERATION_FLOOR_REGRESSION"
    PACKAGE_INVALID = "WB_PACKAGE_INVALID"
    NOT_LOADED = "WB_STATE_NOT_LOADED"
    STATE_CORRUPT = "WB_STATE_CORRUPT"
    LOCK_BUSY = "WB_LOCK_BUSY"
    VERIFY_MISMATCH = "WB_VERIFY_MISMATCH"
    CLI_USAGE = "WB_CLI_USAGE"


_EXIT_CODES = {
    Code.OK: 0,
    Code.VERSION_ROLLBACK: 3,
    Code.VERSION_CONFLICT: 3,
    Code.GENERATION_FLOOR: 3,
    Code.NOT_LOADED: 5,
    Code.VERIFY_MISMATCH: 6,
    Code.LOCK_BUSY: 7,
    Code.STATE_CORRUPT: 8,
}


def exit_code_for(code: str) -> int:
    if code in _EXIT_CODES:
        return _EXIT_CODES[code]
    if code.startswith("WB_") and ("INVALID" in code or "DUPLICATE" in code or "FIELD" in code or "SEQ" in code or "TIME" in code or "HASH" in code or "MONOTONIC" in code or "UNKNOWN" in code or "CYCLE" in code or "COVERAGE" in code or "MISSING" in code):
        return 2
    return 1


class WorkbenchError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_payload(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }
