from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_MESSAGES = {
    "E_FRAME_MISMATCH": "computed frame count does not match observation frame count",
    "E_WINDOW_INVALID": "noise estimation window has insufficient samples",
    "E_STEP_INVALID": "sliding window step must be positive",
    "E_LABEL_INVALID": "attribution produced an uncontrolled label",
    "E_SEGMENT_SPLIT": "truth label length crosses the call frame boundary",
    "E_EVENT_RANGE": "event falls outside the call duration",
    "E_EVENT_OVERLAP": "events overlap on the timeline",
    "E_SNAPSHOT_ROLLBACK": "snapshot version would replace a newer committed version",
    "E_SNAPSHOT_CORRUPT": "committed snapshot failed checksum or is missing data",
    "E_LINEAGE_BROKEN": "lineage chain is incomplete or replay result differs",
    "E_INCONSISTENT": "cross-stage consistency check failed",
    "E_CONFIG_INVALID": "configuration is invalid",
    "E_INPUT_INVALID": "input data is missing or invalid",
    "E_INTERNAL": "unexpected internal error",
}


@dataclass
class BusinessError(Exception):
    code: str
    context: dict[str, Any] = field(default_factory=dict)
    message: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message or ERROR_MESSAGES.get(self.code, self.code))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message or ERROR_MESSAGES.get(self.code, self.code),
            "context": self.context,
        }


def fail(code: str, **context: Any) -> None:
    raise BusinessError(code=code, context=context)

