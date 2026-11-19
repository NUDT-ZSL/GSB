"""Business errors exposed by the sequential message engine."""

from __future__ import annotations

from typing import Any


class EngineError(Exception):
    """A deterministic, machine-readable engine failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "details": self.details,
            "message": self.message,
        }


E_INPUT_JSON = "E_INPUT_JSON"
E_SCHEMA = "E_SCHEMA"
E_TOPIC_DUPLICATE = "E_TOPIC_DUPLICATE"
E_TOPIC_CONFLICT = "E_TOPIC_CONFLICT"
E_PARTITION_LIMIT = "E_PARTITION_LIMIT"
E_PARTITION_CONFIG = "E_PARTITION_CONFIG"
E_PARTITION_NOT_FOUND = "E_PARTITION_NOT_FOUND"
E_ROUTING_RULE = "E_ROUTING_RULE"
E_ROUTING_CONFLICT = "E_ROUTING_CONFLICT"
E_BATCH_FIELD = "E_BATCH_FIELD"
E_BATCH_SEQ = "E_BATCH_SEQ"
E_BATCH_CONFLICT = "E_BATCH_CONFLICT"
E_BATCH_TOO_LARGE = "E_BATCH_TOO_LARGE"
E_TX_DECLARATION = "E_TX_DECLARATION"
E_TX_CONFLICT = "E_TX_CONFLICT"
E_PREPARE_TX_CONFLICT = "E_PREPARE_TX_CONFLICT"
E_TX_NOT_FOUND = "E_TX_NOT_FOUND"
E_TX_STATE = "E_TX_STATE"
E_TX_TIMEOUT = "E_TX_TIMEOUT"
E_PAYLOAD_DECODE = "E_PAYLOAD_DECODE"
E_LOG_FULL = "E_APPEND_LOG_FULL"
E_COMMIT_DIGEST_MISMATCH = "E_COMMIT_DIGEST_MISMATCH"
E_CONSUMER_ID = "E_CONSUMER_ID"
E_CONSUMER_OFFSET = "E_ACK_OFFSET_REGRESSION"
E_ACK_UNCOMMITTED = "E_ACK_UNCOMMITTED"
E_ACK_NOT_DELIVERED = "E_ACK_NOT_DELIVERED"
E_ACK_GAP = "E_ACK_OFFSET_GAP"
E_CLOCK_REGRESSION = "E_CLOCK_REGRESSION"
E_CONFIG_CONFLICT = "E_CONFIG_CONFLICT"
E_INJECTION_FILE = "E_INJECTION_FILE"
E_INJECTION_CONFIG = "E_INJECTION_CONFIG"
E_INJECTION_CONFLICT = "E_INJECTION_CONFLICT"
E_DATABASE = "E_DATABASE"
E_IO = "E_IO"
E_INTERNAL = "E_INTERNAL"

