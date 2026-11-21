from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
    FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    DEPREL_INVALID = "E_DEPREL_INVALID"
    HEAD_RANGE = "E_HEAD_RANGE"
    ROOT_VIOLATION = "E_ROOT_VIOLATION"
    CYCLE_DETECTED = "E_CYCLE_DETECTED"
    SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
    ROUNDTRIP_MISMATCH = "E_ROUNDTRIP_MISMATCH"
    LABEL_RETIRED = "E_LABEL_RETIRED"
    SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    DUP_SENT = "E_DUP_SENT"
    TOKEN_SEQ = "E_TOKEN_SEQ"
    POS_INVALID = "E_POS_INVALID"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    INCONSISTENT = "E_INCONSISTENT"


@dataclass
class BusinessError(Exception):
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code.value}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            },
        }


def fail(code: ErrorCode, message: str, **details: Any) -> None:
    raise BusinessError(code=code, message=message, details=details)


def error_result(exc: BusinessError) -> dict[str, Any]:
    return exc.to_dict()
