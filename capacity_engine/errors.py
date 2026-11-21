from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


EXIT_VALIDATION = 2
EXIT_STATE = 3
EXIT_RUNTIME = 4


ERROR_CATALOG: dict[str, dict[str, Any]] = {
    "E_SCENE_JSON": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_SCENE_SHAPE": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_DUPLICATE_SERVICE": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_DUPLICATE_TENANT": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_DUPLICATE_BATCH": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_UNKNOWN_TENANT": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_UNKNOWN_SERVICE": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_SERVICE_TENANT_MISMATCH": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_ADDRESS": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_GRADIENT": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_DURATION": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_PACE": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_BATCH": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_SLO": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_SCALE": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_TOLERANCE": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_BUDGET": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_CLOCK": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_INVALID_FIELD": {"category": "validation", "exit_code": EXIT_VALIDATION},
    "E_DB_NOT_FOUND": {"category": "state", "exit_code": EXIT_STATE},
    "E_SCENE_MISMATCH": {"category": "state", "exit_code": EXIT_STATE},
    "E_SCHEMA_MISMATCH": {"category": "state", "exit_code": EXIT_STATE},
    "E_UNIT_NOT_CLAIMED": {"category": "state", "exit_code": EXIT_STATE},
    "E_WINDOW_SUMMARY_MISMATCH": {"category": "state", "exit_code": EXIT_STATE},
    "E_INCOMPLETE_RUN": {"category": "state", "exit_code": EXIT_STATE},
    "E_CONNECTION_REFUSED": {"category": "runtime", "exit_code": EXIT_RUNTIME},
    "E_REQUEST_BUDGET_EXCEEDED": {"category": "runtime", "exit_code": EXIT_RUNTIME},
    "E_STATUS_CODE_MISMATCH": {"category": "runtime", "exit_code": EXIT_RUNTIME},
    "E_MALFORMED_RESPONSE": {"category": "runtime", "exit_code": EXIT_RUNTIME},
    "E_TARGET_INTERRUPTED": {"category": "runtime", "exit_code": EXIT_RUNTIME},
    "E_VERIFY_FAILED": {"category": "runtime", "exit_code": EXIT_RUNTIME},
    "E_INTERNAL": {"category": "runtime", "exit_code": EXIT_RUNTIME},
}


@dataclass
class BizError(Exception):
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_CATALOG:
            self.code = "E_INTERNAL"
        if self.exit_code is None:
            self.exit_code = int(ERROR_CATALOG[self.code]["exit_code"])
        super().__init__(f"{self.code}: {self.message}")

    @property
    def category(self) -> str:
        return str(ERROR_CATALOG[self.code]["category"])

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "category": self.category,
                "message": self.message,
                "details": dict(self.details),
            },
        }


def validation_error(code: str, message: str, **details: Any) -> BizError:
    return BizError(code=code, message=message, details=details)

