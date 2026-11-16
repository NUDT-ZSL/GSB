"""Business errors with stable machine-readable codes."""

from __future__ import annotations

from typing import Any, Mapping, Optional


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CARDINALITY_EXCEEDED = "E_CARDINALITY_EXCEEDED"
E_BUDGET_EXHAUSTED = "E_BUDGET_EXHAUSTED"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_COMMAND_INVALID = "E_COMMAND_INVALID"
E_EVALUATION_FAILED = "E_EVALUATION_FAILED"


class FeatureLabError(Exception):
    """An expected application failure that should be reported, not leaked."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def config_error(message: str, **details: Any) -> FeatureLabError:
    return FeatureLabError(E_CONFIG_INVALID, message, details=details)
