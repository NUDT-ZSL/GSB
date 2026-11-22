"""Structured business errors used by every domain module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_TYPE_SET_CORRUPT = "E_TYPE_SET_CORRUPT"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_ANNOTATION_CORRUPT = "E_ANNOTATION_CORRUPT"
E_TYPE_NOT_ALLOWED = "E_TYPE_NOT_ALLOWED"
E_ALIAS_UNRESOLVED = "E_ALIAS_UNRESOLVED"
E_BIO_INVALID = "E_BIO_INVALID"
E_TYPE_RETIRED = "E_TYPE_RETIRED"
E_DOC_REJECTED = "E_DOC_REJECTED"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_TRACE_BROKEN = "E_TRACE_BROKEN"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCONSISTENT = "E_INCONSISTENT"


@dataclass
class GovernanceError(Exception):
    """A machine-readable business failure.

    ``details`` intentionally contains only JSON-compatible values so callers
    can persist the complete failure without translating implementation types.
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def fail(
    code: str,
    message: str,
    *,
    field: str | None = None,
    line: int | None = None,
    doc_id: str | None = None,
    position: int | None = None,
    label: str | None = None,
    **extra: Any,
) -> None:
    """Raise a :class:`GovernanceError` with common, stable context fields."""

    details: dict[str, Any] = {}
    if field is not None:
        details["field"] = field
    if line is not None:
        details["line"] = line
    if doc_id is not None:
        details["doc_id"] = doc_id
    if position is not None:
        details["position"] = position
    if label is not None:
        details["label"] = label
    details.update(extra)
    raise GovernanceError(code, message, details)
