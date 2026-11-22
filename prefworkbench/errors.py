"""Structured business errors used across CLI, pipeline, and worker boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    stage: str | None = None
    pair_ids: list[str] = field(default_factory=list)
    batch_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.stage is not None:
            result["stage"] = self.stage
        if self.pair_ids:
            result["pair_ids"] = sorted(self.pair_ids)
        if self.batch_id is not None:
            result["batch_id"] = self.batch_id
        if self.details:
            result["details"] = self.details
        return result


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_INPUT_INVALID = "E_INPUT_INVALID"
E_DUPLICATE_PAIR_ID = "E_DUPLICATE_PAIR_ID"
E_VERSION_REGRESSION = "E_VERSION_REGRESSION"
E_CHECKSUM_MISMATCH = "E_CHECKSUM_MISMATCH"
E_STALE_RESULT = "E_STALE_RESULT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_WORKER_FAILURE = "E_WORKER_FAILURE"
E_BUDGET_FLOP_EXCEEDED = "E_BUDGET_FLOP_EXCEEDED"
E_BUDGET_MEMORY_EXCEEDED = "E_BUDGET_MEMORY_EXCEEDED"
E_BUDGET_WORKER_SLOT = "E_BUDGET_WORKER_SLOT"
W_DEGRADED_GROUPING = "W_DEGRADED_GROUPING"
E_LABEL_CROSS_CONTRADICTION = "E_LABEL_CROSS_CONTRADICTION"
E_GROUP_DIRECTION_CONFLICT = "E_GROUP_DIRECTION_CONFLICT"
E_IDENTICAL_SIDE_CONTRADICTION = "E_IDENTICAL_SIDE_CONTRADICTION"
E_SELFCHECK_FAILED = "E_SELFCHECK_FAILED"

