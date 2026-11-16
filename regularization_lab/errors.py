"""Stable business failures and deterministic ordering helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_NUMERIC_DRIFT = "E_NUMERIC_DRIFT"
E_BATCH_FAILURE = "E_BATCH_FAILURE"
E_UPDATE_FAILURE = "E_UPDATE_FAILURE"
E_SNAPSHOT_FAILURE = "E_SNAPSHOT_FAILURE"
E_SNAPSHOT_INVALID = "E_SNAPSHOT_INVALID"
E_CACHE_INVALID = "E_CACHE_INVALID"
E_COMMAND_INVALID = "E_COMMAND_INVALID"


@dataclass(frozen=True)
class Failure:
    """One isolated, auditable failure.

    ``phase`` is a stable machine-readable stage name.  ``round`` and
    ``batch_index`` are 1-based when present.  Failures are sorted by a stable
    key rather than thread completion order.
    """

    business_code: str
    phase: str
    message: str
    exception_type: str = "ConfigValidationError"
    scheme: str | None = None
    round: int | None = None
    batch_index: int | None = None
    stage: str | None = None
    column: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.round if self.round is not None else -1,
            self.batch_index if self.batch_index is not None else -1,
            self.phase,
            self.stage or "",
            self.business_code,
            self.scheme or "",
            self.column or "",
            self.message,
            self.exception_type,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_code": self.business_code,
            "phase": self.phase,
            "stage": self.stage or self.phase,
            "exception_type": self.exception_type,
            "scheme": self.scheme,
            "round": self.round,
            "batch_index": self.batch_index,
            "column": self.column,
            "message": self.message,
            "details": self.details,
        }


def sorted_failures(failures: Iterable[Failure]) -> list[Failure]:
    return sorted(failures, key=Failure.sort_key)


def business_codes(failures: Iterable[Failure]) -> list[str]:
    return sorted({item.business_code for item in failures})


def failure_counts(failures: Iterable[Failure]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in failures:
        counts[item.business_code] = counts.get(item.business_code, 0) + 1
    return {key: counts[key] for key in sorted(counts)}
