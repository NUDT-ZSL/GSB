"""Business error model shared by stages, workers and the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_DUP_SAMPLE = "E_DUP_SAMPLE"
E_BAD_FIELD = "E_BAD_FIELD"
E_TEMPLATE_PLACEHOLDER = "E_TEMPLATE_PLACEHOLDER"
E_TEMPLATE_NOT_FOUND = "E_TEMPLATE_NOT_FOUND"
E_CONCURRENCY_LIMIT = "E_CONCURRENCY_LIMIT"
E_COMPARE_BUDGET = "E_COMPARE_BUDGET"
E_MEMORY_LIMIT = "E_MEMORY_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_STALE_VERSION = "E_STALE_VERSION"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_WORKER_FAILURE = "E_WORKER_FAILURE"
E_FORCED_TERMINATION = "E_FORCED_TERMINATION"
E_INVALID_INPUT = "E_INVALID_INPUT"


@dataclass
class BusinessError(Exception):
    """An error with a stable code, stage/batch attribution and JSON details."""

    code: str
    message: str
    stage: str | None = None
    batch_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def with_context(
        self,
        *,
        stage: str | None = None,
        batch_id: str | None = None,
    ) -> "BusinessError":
        if self.stage is None and stage is not None:
            self.stage = stage
        if self.batch_id is None and batch_id is not None:
            self.batch_id = batch_id
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "batch_id": self.batch_id,
            "details": self.details,
        }


class ForcedTermination(Exception):
    """Test/CLI failpoint used to model a forced process termination."""

    def __init__(self, stage: str, reason: str = "forced termination") -> None:
        super().__init__(f"{stage}: {reason}")
        self.stage = stage
        self.reason = reason

