from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXIT_CODES = {
    "E_VALIDATION": 2,
    "E_INPUT_MISSING": 2,
    "E_DUPLICATE_LOGICAL_TABLE": 2,
    "E_SHARD_KEY_MISSING": 2,
    "E_RULE_TYPE_INVALID": 2,
    "E_MODULUS_INVALID": 2,
    "E_BOUNDARIES_INVALID": 2,
    "E_MAPPING_INCOMPLETE": 2,
    "E_SHARD_DB_UNOPENABLE": 2,
    "E_V1_EFFECT_INVALID": 2,
    "E_V2_CLOCK_INVALID": 2,
    "E_V2_DIGEST_MISMATCH": 2,
    "E_V2_GENERATION_NOT_MONOTONIC": 2,
    "E_RULE_ROUTING_MISSING": 2,
    "E_TEMPLATE_INVALID": 2,
    "E_INJECTION_INVALID": 2,
    "E_STATE_NOT_LOADED": 3,
    "E_STATE_CORRUPT": 3,
    "E_QUERY_NOT_FOUND": 3,
    "E_QUERY_ID_CONFLICT": 4,
    "E_VERSION_CONFLICT": 4,
    "E_RULE_VERSION_CONFLICT": 4,
    "E_CLOCK_REGRESSION": 4,
    "E_ROLLBACK_REJECTED": 4,
    "E_RULE_NOT_FOUND": 4,
    "E_SHARD_KEY_OUT_OF_RANGE": 5,
    "E_PHYSICAL_TABLE_MISSING": 5,
    "E_SHARD_RESULT_DIGEST_MISMATCH": 5,
    "E_EXECUTION_FAILED": 5,
    "E_MERGE_FAILED": 5,
    "E_AGGREGATE_FAILED": 5,
    "E_QUERY_INCOMPLETE": 5,
    "E_RESUME_UNSAFE": 5,
    "E_INTERNAL": 9,
}


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "exit_code": EXIT_CODES.get(self.code, 1),
        }

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 1)


def fail(code: str, message: str, **details: Any) -> None:
    raise BusinessError(code=code, message=message, details=details)
