"""Business error codes used by every layer of the diagnostic engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_BAD_DOC = "E_BAD_DOC"
E_POSTING_INVALID = "E_POSTING_INVALID"
E_COMPRESS_PARAM = "E_COMPRESS_PARAM"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_DUP_DOC = "E_DUP_DOC"
E_BATCH_ROLLBACK = "E_BATCH_ROLLBACK"
E_CONFIG_ROLLBACK = "E_CONFIG_ROLLBACK"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_STAGE_INCOMPLETE = "E_STAGE_INCOMPLETE"
E_TERM_NOT_FOUND = "E_TERM_NOT_FOUND"
E_FAULT_INJECTED = "E_FAULT_INJECTED"
E_IO = "E_IO"


@dataclass
class DiagnosticError(Exception):
    """An expected, machine-readable business failure."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload
