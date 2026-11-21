"""Structured domain errors used by the public evaluation API."""

from __future__ import annotations

from typing import Any, Iterable


E_INVALID_SAMPLE = "E_INVALID_SAMPLE"
E_DUPLICATE_SAMPLE_ID = "E_DUPLICATE_SAMPLE_ID"
E_CANVAS_MISMATCH = "E_CANVAS_MISMATCH"
E_EMPTY_PROMPT = "E_EMPTY_PROMPT"
E_UNKNOWN_PRECISION = "E_UNKNOWN_PRECISION"
E_INVALID_LATENCY_BUDGET = "E_INVALID_LATENCY_BUDGET"
E_INVALID_MEMORY_LIMIT = "E_INVALID_MEMORY_LIMIT"
E_INVALID_CONFIG = "E_INVALID_CONFIG"
E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCONSISTENT = "E_INCONSISTENT"
E_UNATTRIBUTED = "E_UNATTRIBUTED"
E_STAGE_FAILURE = "E_STAGE_FAILURE"


class DomainError(Exception):
    """A business error with machine-readable fields and optional stage."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        sample_id: str | None = None,
        stage: str | None = None,
        category: str | None = None,
        degradation_category: str | None = None,
        completed_stages: Iterable[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.sample_id = sample_id
        self.stage = stage
        self.category = category
        self.degradation_category = degradation_category
        self.completed_stages = tuple(completed_stages or ())
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "sample_id": self.sample_id,
            "stage": self.stage,
            "category": self.category,
            "degradation_category": self.degradation_category,
            "completed_stages": list(self.completed_stages),
        }
        if self.details:
            payload["details"] = self.details
        return payload


class StageFailure(Exception):
    """Internal wrapper that attributes an unexpected exception to a stage."""

    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(f"{stage}: {type(cause).__name__}: {cause}")
        self.stage = stage
        self.cause = cause

