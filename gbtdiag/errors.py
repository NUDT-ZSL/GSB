"""Domain error codes and the unified domain exception.

Every failure raised by the component is expressed with one of the
business codes defined here so that it can be recorded, propagated through
the staged pipeline and surfaced in the final diagnostic report instead of
being swallowed.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional


class ErrorCode(str, Enum):
    """Stable business codes used by every stage of the pipeline."""

    OK = "E_OK"
    CONFIG_INVALID = "E_CONFIG_INVALID"
    CATEGORY_OUT_OF_RANGE = "E_CATEGORY_OUT_OF_RANGE"
    DUPLICATE_THRESHOLD = "E_DUPLICATE_THRESHOLD"
    NONFINITE_HESSIAN = "E_NONFINITE_HESSIAN"
    NEGATIVE_LEARNING_RATE = "E_NEGATIVE_LEARNING_RATE"
    TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
    INCONSISTENT = "E_INCONSISTENT"
    UNATTRIBUTED = "E_UNATTRIBUTED"


class DomainError(Exception):
    """An exception carrying a structured, serialisable payload.

    Attributes
    ----------
    code:
        One of :class:`ErrorCode`.
    stage:
        Pipeline stage that produced the error
        (``binning`` / ``gain`` / ``leaf`` / ``prediction`` / ``config`` ...).
    sample_indices:
    Indices of the rows involved in the fault (empty list when not row level).
    field:
        Configuration field name when the fault is configuration related.
    node_path:
        Tree node path when the fault is produced inside a leaf solver.
    details:
        Extra structured context.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        stage: str = "unknown",
        sample_indices: Optional[list] = None,
        field: Optional[str] = None,
        node_path: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.sample_indices = list(sample_indices) if sample_indices else []
        self.field = field
        self.node_path = node_path
        self.details = dict(details) if details else {}

    def to_record(self) -> Dict[str, Any]:
        """Return the canonical dict representation used by reports/hashes."""
        return {
            "code": self.code.value,
            "stage": self.stage,
            "message": self.message,
            "sample_indices": list(self.sample_indices),
            "field": self.field,
            "node_path": self.node_path,
            "details": self.details,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"DomainError({self.code.value}, stage={self.stage!r}, "
            f"message={self.message!r})"
        )
