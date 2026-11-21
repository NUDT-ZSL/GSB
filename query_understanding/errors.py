"""Business error types and stable error codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_MAP_MISSING = "E_MAP_MISSING"
E_DICT_TRUNCATED = "E_DICT_TRUNCATED"
E_PATH_INCOMPARABLE = "E_PATH_INCOMPARABLE"
E_MATCH_BUDGET = "E_MATCH_BUDGET"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_INCONSISTENT = "E_INCONSISTENT"
E_INPUT_INVALID = "E_INPUT_INVALID"
E_TAXONOMY_INVALID = "E_TAXONOMY_INVALID"
E_METRIC_MISMATCH = "E_METRIC_MISMATCH"

INJECTION_CODES = {
    "normalize": "E_INJECT_NORMALIZE",
    "tokenize": "E_INJECT_TOKENIZE",
    "slot": "E_INJECT_SLOT",
    "intent": "E_INJECT_INTENT",
}


@dataclass
class QUError(Exception):
    """A domain error carrying a stable machine-readable code."""

    code: str
    message: str
    stage: str | None = None
    category: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
        }
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.category is not None:
            payload["category"] = self.category
        if self.details:
            payload["details"] = self.details
        return payload


def error_dict(
    code: str,
    message: str,
    *,
    stage: str | None = None,
    category: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return QUError(
        code=code,
        message=message,
        stage=stage,
        category=category,
        details=details or {},
    ).to_dict()

