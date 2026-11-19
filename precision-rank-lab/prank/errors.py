"""Structured business error codes and exception chain handling.

Every recoverable business failure is represented by :class:`BusinessError`
carrying an ``E_xxx`` code, the pipeline stage, an optional ``query_id`` and a
chain of upstream codes so degradation never silently swallows a root cause.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

#: Exit codes stay in the portable 0..255 range (Windows compatible).
EXIT_CODE_BASE = 20

BUSINESS_CODES: dict[str, int] = {
    "E_DUP_QUERY": 1,
    "E_ITEM_EMPTY": 2,
    "E_VALUE_RANGE": 3,
    "E_PRECISION_UNKNOWN": 4,
    "E_SCALE_INVALID": 5,
    "E_CONFIG_INVALID": 6,
    "E_INCONSISTENT": 7,
    "E_SNAPSHOT_CORRUPT": 8,
    "E_INPUT_INVALID": 9,
}


def exit_code_for(business_code: str) -> int:
    """Map an ``E_xxx`` business code to a portable integer process exit code."""
    return EXIT_CODE_BASE + BUSINESS_CODES.get(business_code, 99)


@dataclass
class BusinessError(Exception):
    """Structured business error with traceable stage / query / upstream chain."""

    code: str
    stage: str
    message: str
    query_id: str | None = None
    field: str | None = None
    context: dict[str, Any] = dc_field(default_factory=dict)
    upstream: list["BusinessError"] = dc_field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "query_id": self.query_id,
            "field": self.field,
            "context": self.context,
            "upstream_chain": [e.code for e in self.upstream],
            "upstream_detail": [e.to_dict() for e in self.upstream],
        }

    @classmethod
    def chain(cls, downstream: "BusinessError", upstream: "BusinessError") -> "BusinessError":
        """Attach an upstream cause to a downstream degradation error."""
        downstream.upstream = list(upstream.upstream) + [upstream]
        return downstream
