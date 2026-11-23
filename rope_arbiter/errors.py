"""Structured domain errors used by the rope arbitration component."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_INVALID_CASE = "E_INVALID_CASE"
E_MAPPING_UNDECLARED = "E_MAPPING_UNDECLARED"
E_MAPPING_DRIFT = "E_MAPPING_DRIFT"
E_BLOCK_MISMATCH = "E_BLOCK_MISMATCH"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_MISSING_CASE = "E_SNAPSHOT_MISSING_CASE"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_INTERNAL = "E_INTERNAL"


@dataclass(frozen=True)
class DomainError(Exception):
    """An error that can be serialized without losing business context."""

    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "context": dict(self.context),
        }


def as_domain_error(exc: BaseException) -> DomainError:
    if isinstance(exc, DomainError):
        return exc
    return DomainError(E_INTERNAL, str(exc), {"exception_type": type(exc).__name__})
