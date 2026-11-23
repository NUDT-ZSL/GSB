from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    INVALID_JSON = "CS_INVALID_JSON"
    SCHEMA_INVALID = "CS_SCHEMA_INVALID"
    DUPLICATE_INSTANCE = "CS_DUPLICATE_INSTANCE"
    DUPLICATE_LAYER = "CS_DUPLICATE_LAYER"
    DUPLICATE_NODE = "CS_DUPLICATE_NODE"
    DUPLICATE_REQUIRED_LAYER = "CS_DUPLICATE_REQUIRED_LAYER"
    LAYER_MISSING = "CS_LAYER_MISSING"
    LAYER_DIGEST_MISMATCH = "CS_LAYER_DIGEST_MISMATCH"
    NODE_MISSING = "CS_NODE_MISSING"
    INSTANCE_NODE_AMBIGUOUS = "CS_INSTANCE_NODE_AMBIGUOUS"
    INVALID_INTEGER = "CS_INVALID_INTEGER"
    INVALID_QUANTITY = "CS_INVALID_QUANTITY"
    INVALID_CONFIG = "CS_INVALID_CONFIG"
    LAYER_BYTES_EXCEED_NODE = "CS_LAYER_BYTES_EXCEED_NODE"
    INPUT_FINGERPRINT_MISMATCH = "CS_INPUT_FINGERPRINT_MISMATCH"
    INVALID_STAGE = "CS_INVALID_STAGE"
    INVALID_TRANSITION = "CS_INVALID_TRANSITION"
    NO_CAPACITY = "CS_NO_CAPACITY"
    CLOCK_ROLLBACK = "CS_CLOCK_ROLLBACK"
    IDEMPOTENCY_CONFLICT = "CS_IDEMPOTENCY_CONFLICT"
    CONSERVATION_VIOLATION = "CS_CONSERVATION_VIOLATION"
    LEDGER_CORRUPTION = "CS_LEDGER_CORRUPTION"
    INTERNAL_ERROR = "CS_INTERNAL_ERROR"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

