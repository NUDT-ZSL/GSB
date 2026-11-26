"""Structured business errors shared by every workbench module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


OK = "OK"
E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_SEMANTIC_LOSS = "E_SEMANTIC_LOSS"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_LEDGER_CONFLICT = "E_LEDGER_CONFLICT"
E_INCONSISTENT = "E_INCONSISTENT"
E_RESOURCE_ADJUSTED = "E_RESOURCE_ADJUSTED"
E_BUDGET_EXCEEDED = "E_BUDGET_EXCEEDED"
E_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
E_REQUEST_INVALID = "E_REQUEST_INVALID"
E_NO_ROUTE = "E_NO_ROUTE"


@dataclass
class WorkbenchError(Exception):
    """An exception that always carries a stable machine-readable code."""

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    first_request_id: str | None = None
    first_node: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }
        if self.first_request_id is not None:
            payload["first_request_id"] = self.first_request_id
        if self.first_node is not None:
            payload["first_node"] = self.first_node
        return payload


def corrupt(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(E_FORMAT_CORRUPT, message, details)


def unsupported(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(E_VERSION_UNSUPPORTED, message, details)

