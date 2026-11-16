"""Business error definitions shared by all execution paths."""

from __future__ import annotations

from typing import Any, Mapping


BUSINESS_CODES = frozenset(
    {
        "E_NONFINITE_INPUT",
        "E_SHAPE_MISMATCH",
        "E_TILE_INVALID",
        "E_BUDGET_INVALID",
        "E_CONFIG_INVALID",
        "E_CACHE_KEY_INVALID",
        "E_INJECTION_INVALID",
        "E_NUMERIC_INCONSISTENCY",
        "E_CONCURRENCY_INCONSISTENCY",
        "E_CACHE_INCONSISTENCY",
    }
)


class GemmError(RuntimeError):
    """A typed failure that can be serialized into the audit report."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if code not in BUSINESS_CODES:
            raise ValueError(f"unknown business code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def error_record(
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return GemmError(code, message, details).as_dict()
