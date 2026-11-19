"""Stable business error contract used by the CLI, app, and tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_SPEAKER_INVALID = "E_SPEAKER_INVALID"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_INCONSISTENT = "E_INCONSISTENT"


@dataclass(slots=True)
class BusinessError(Exception):
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": dict(self.details),
            },
        }

