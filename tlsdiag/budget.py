"""Cooperative real-wall-clock deadlines and budget exceptions."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class EngineError(Exception):
    code = "E_INTERNAL"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: Dict[str, Any] = detail


class DeadlineExceeded(EngineError):
    code = "E_TIMEOUT"


class FragmentLimitExceeded(EngineError):
    code = "E_FRAGS"


class ExpandedBudgetExceeded(EngineError):
    code = "E_BUDGET"


class LockConflict(EngineError):
    code = "E_LOCK"


@dataclass
class Deadline:
    """A monotonic deadline measured with the real wall clock."""

    limit_ms: int
    started: float = field(default_factory=time.perf_counter)
    stage: str = "start"
    message_seq: Optional[int] = None

    @property
    def limit_seconds(self) -> float:
        return max(0, self.limit_ms) / 1000.0

    @property
    def expires_at(self) -> float:
        return self.started + self.limit_seconds

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000.0

    def remaining_ms(self) -> float:
        return max(0.0, self.expires_at - time.perf_counter()) * 1000.0

    def check(self, stage: Optional[str] = None, message_seq: Optional[int] = None) -> None:
        if stage is not None:
            self.stage = stage
        if message_seq is not None:
            self.message_seq = message_seq
        if time.perf_counter() >= self.expires_at:
            raise DeadlineExceeded(
                "connection exceeded the real wall-clock processing limit",
                stage=self.stage,
                message_seq=self.message_seq,
                limit_ms=self.limit_ms,
                elapsed_ms=round(self.elapsed_ms(), 3),
            )
