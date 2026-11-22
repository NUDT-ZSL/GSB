from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineError(Exception):
    code: str
    message: str
    event_seq: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        location = f" at event {self.event_seq}" if self.event_seq is not None else ""
        return f"{self.code}{location}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "event_seq": self.event_seq,
            "details": self.details,
        }


E_BAD_SCENE = "E_BAD_SCENE"
E_ACK_RANGE = "E_ACK_RANGE"
E_FC_REGRESS = "E_FC_REGRESS"
E_PTO_UNSENT = "E_PTO_UNSENT"
E_PTO_BACKOFF_LIMIT = "E_PTO_BACKOFF_LIMIT"
E_STREAM_LIMIT = "E_STREAM_LIMIT"
E_CONNECTION_CLOSED = "E_CONNECTION_CLOSED"
E_TRACE_DIVERGENCE = "E_TRACE_DIVERGENCE"
E_STATE_EXISTS = "E_STATE_EXISTS"
E_ALREADY_COMPLETE = "E_ALREADY_COMPLETE"
E_NO_STATE = "E_NO_STATE"
E_SCENE_MISMATCH = "E_SCENE_MISMATCH"
E_RECOVERY = "E_RECOVERY"
E_DB = "E_DB"
E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"

