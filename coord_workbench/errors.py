"""Business error codes with diagnosable fields.

Every validation / merge / persistence failure raises :class:`BizError`
carrying a stable, machine-readable ``code`` plus a ``fields`` dict that
preserves the offending values for diagnostics.
"""

from __future__ import annotations

from typing import Any, Dict


class Err:
    # --- manifest / event / subscription validation (req 1) ---
    FIELD_MISSING = "E_FIELD_MISSING"
    NODE_DUPLICATE = "E_NODE_DUPLICATE"
    ROLE_ENUM = "E_ROLE_ENUM"
    HEALTH_ENUM = "E_HEALTH_ENUM"
    ADDR_NOT_LOOPBACK = "E_ADDR_NOT_LOOPBACK"
    EPOCH_REGRESS = "E_EPOCH_REGRESS"
    EVENT_SEQ_ORDER = "E_EVENT_SEQ_ORDER"
    TERM_REGRESS = "E_TERM_REGRESS"
    V1_REL_SECONDS = "E_V1_REL_SECONDS"
    SHARD_HASH_MISMATCH = "E_SHARD_HASH_MISMATCH"
    EPOCH_CONFLICT = "E_EPOCH_CONFLICT"
    SUBSCRIBER_UNKNOWN = "E_SUBSCRIBER_UNKNOWN"
    CALLBACK_NOT_LOOPBACK = "E_CALLBACK_NOT_LOOPBACK"
    # --- merge / clock (req 2, 3) ---
    CLOCK_REGRESS = "E_CLOCK_REGRESS"
    MERGE_ABORTED = "E_MERGE_ABORTED"
    # --- scheduler (req 4) ---
    BATCH_DUPLICATE = "E_BATCH_DUPLICATE"
    # --- state machine / persistence (req 5, 6) ---
    LEASE_REGRESS = "E_LEASE_REGRESS"
    VERSION_REGRESS = "E_VERSION_REGRESS"
    CHAIN_BROKEN = "E_CHAIN_BROKEN"
    # --- recovery (req 7) ---
    PAGE_MISALIGNED = "E_PAGE_MISALIGNED"
    LEDGER_CORRUPT = "E_LEDGER_CORRUPT"


class BizError(Exception):
    """Business error carrying a stable code and diagnosable fields."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.fields: Dict[str, Any] = fields

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "fields": self.fields}
