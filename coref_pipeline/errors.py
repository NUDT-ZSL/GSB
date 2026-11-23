"""Business errors and deterministic error serialization."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


E_DUP_EVENT = "E_DUP_EVENT"
E_TYPE_INVALID = "E_TYPE_INVALID"
E_OFFSET_INVALID = "E_OFFSET_INVALID"
E_ROLE_INVALID = "E_ROLE_INVALID"
E_EVIDENCE_CONFIG = "E_EVIDENCE_CONFIG"
E_LINK_CONFLICT = "E_LINK_CONFLICT"
E_ORPHAN_EVENT = "E_ORPHAN_EVENT"
E_ARBITRATION_UNDECIDED = "E_ARBITRATION_UNDECIDED"
E_INCONSISTENT = "E_INCONSISTENT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_INPUT_INVALID = "E_INPUT_INVALID"
E_INTERNAL = "E_INTERNAL"


@dataclass
class PipelineError(Exception):
    """A machine-readable, stage-attributed pipeline failure.

    ``event_id`` is populated whenever an error can be attributed to one event.
    Validation errors never silently skip records: the caller receives this
    exception and the CLI writes it to the report.
    """

    code: str
    stage: str
    message: str
    event_id: str | None = None
    field: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
        }
        if self.event_id is not None:
            payload["event_id"] = self.event_id
        if self.field is not None:
            payload["field"] = self.field
        if self.details:
            payload["details"] = self.details
        return payload


def error_report(error: PipelineError) -> dict[str, Any]:
    return error.to_dict()

