"""Business errors with stable, machine-readable codes."""

from __future__ import annotations

from typing import Any


class SeqLedgerError(Exception):
    """Base class for all user-facing failures."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: dict[str, Any] | None = None,
        exit_code: int = 1,
        ledger_written: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = diagnostics or {}
        self.exit_code = exit_code
        self.ledger_written = ledger_written

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "diagnostics": self.diagnostics,
                "ledger_written": self.ledger_written,
            },
        }


class InputError(SeqLedgerError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            diagnostics=diagnostics,
            exit_code=2,
            ledger_written=False,
        )


class StoreError(SeqLedgerError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: dict[str, Any] | None = None,
        ledger_written: bool = False,
    ) -> None:
        super().__init__(
            code,
            message,
            diagnostics=diagnostics,
            exit_code=3,
            ledger_written=ledger_written,
        )


class VerifyError(SeqLedgerError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            diagnostics=diagnostics,
            exit_code=4,
            ledger_written=False,
        )


class ReplayError(SeqLedgerError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: dict[str, Any] | None = None,
        ledger_written: bool = False,
    ) -> None:
        super().__init__(
            code,
            message,
            diagnostics=diagnostics,
            exit_code=5,
            ledger_written=ledger_written,
        )


# Stable business error codes. Keep the string values independent even when the
# underlying validation category is related.
TOPOLOGY_DUPLICATE_TOPIC = "TOPOLOGY_DUPLICATE_TOPIC"
TOPOLOGY_INVALID_PARTITION_COUNT = "TOPOLOGY_INVALID_PARTITION_COUNT"
TOPOLOGY_INVALID_RULE = "TOPOLOGY_INVALID_RULE"
TOPOLOGY_RULE_UNPARSABLE = "TOPOLOGY_RULE_UNPARSABLE"
TOPOLOGY_RULE_UNCOVERED = "TOPOLOGY_RULE_UNCOVERED"
EVENT_DUPLICATE_SEQUENCE = "EVENT_DUPLICATE_SEQUENCE"
EVENT_OUT_OF_ORDER = "EVENT_OUT_OF_ORDER"
EVENT_SCHEMA_INVALID = "EVENT_SCHEMA_INVALID"
PARTITION_MISSING = "PARTITION_MISSING"
PARTITION_KEY_INVALID = "PARTITION_KEY_INVALID"
SUBSCRIPTION_DUPLICATE_ID = "SUBSCRIPTION_DUPLICATE_ID"
SUBSCRIPTION_SCHEMA_INVALID = "SUBSCRIPTION_SCHEMA_INVALID"
SUBSCRIBER_ENDPOINT_INVALID = "SUBSCRIBER_ENDPOINT_INVALID"
LOGICAL_CLOCK_INVALID = "LOGICAL_CLOCK_INVALID"
LOGICAL_CLOCK_ROLLBACK = "LOGICAL_CLOCK_ROLLBACK"
V1_SCHEMA_INVALID = "V1_SCHEMA_INVALID"
V1_RELATIVE_SECONDS_INVALID = "V1_RELATIVE_SECONDS_INVALID"
V1_DUPLICATE_MESSAGE_KEY = "V1_DUPLICATE_MESSAGE_KEY"
V2_SCHEMA_INVALID = "V2_SCHEMA_INVALID"
V2_SHARD_HASH_MISMATCH = "V2_SHARD_HASH_MISMATCH"
V2_GENERATION_REGRESSION = "V2_GENERATION_REGRESSION"
V2_TOMBSTONE_OPEN = "V2_TOMBSTONE_OPEN"
V2_META_INCONSISTENT = "V2_META_INCONSISTENT"
SEMANTIC_GENERATION_REGRESSION = "SEMANTIC_GENERATION_REGRESSION"
MERGE_CONFLICT = "MERGE_CONFLICT"
LEDGER_VERSION_REGRESSION = "LEDGER_VERSION_REGRESSION"
LEDGER_CORRUPT = "LEDGER_CORRUPT"
LEDGER_NOT_INITIALIZED = "LEDGER_NOT_INITIALIZED"
LEDGER_CONFIG_CONFLICT = "LEDGER_CONFIG_CONFLICT"
EVENT_CONFLICT = "EVENT_CONFLICT"
PATCH_SCHEMA_INVALID = "PATCH_SCHEMA_INVALID"
PATCH_GENERATION_REGRESSION = "PATCH_GENERATION_REGRESSION"
PATCH_TARGET_MISSING = "PATCH_TARGET_MISSING"
PATCH_WATERMARK_INVALID = "PATCH_WATERMARK_INVALID"
REPLAY_NO_TOPOLOGY = "REPLAY_NO_TOPOLOGY"
REPLAY_NO_SEMANTICS = "REPLAY_NO_SEMANTICS"
REPLAY_INFLIGHT_CONFLICT = "REPLAY_INFLIGHT_CONFLICT"
DELIVERY_CLAIM_CONFLICT = "DELIVERY_CLAIM_CONFLICT"
ACK_CONFLICT = "ACK_CONFLICT"
VERIFY_MISMATCH = "VERIFY_MISMATCH"
VERIFY_LEDGER_CORRUPT = "VERIFY_LEDGER_CORRUPT"
CONCURRENCY_LOCK_FAILED = "CONCURRENCY_LOCK_FAILED"
