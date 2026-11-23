"""Structured business error codes shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


class ErrorCode:
    E_MERGE_POLICY = "E_MERGE_POLICY"
    E_NORM_INVALID = "E_NORM_INVALID"
    E_WEIGHT_INVALID = "E_WEIGHT_INVALID"
    E_RECALL_BUDGET = "E_RECALL_BUDGET"
    E_FUSION_BUDGET = "E_FUSION_BUDGET"
    E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    E_VALIDATION = "E_VALIDATION"
    E_WORKER = "E_WORKER"


@dataclass
class PipelineError(Exception):
    """Structured error carrying a business code, stage, query and context."""

    code: str
    stage: str
    message: str
    query_id: str | None = None
    field: str | None = None
    batch_id: int | None = None
    context: dict[str, Any] | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        loc = []
        if self.batch_id is not None:
            loc.append(f"batch={self.batch_id}")
        if self.query_id is not None:
            loc.append(f"query_id={self.query_id}")
        if self.field is not None:
            loc.append(f"field={self.field}")
        suffix = f" ({', '.join(loc)})" if loc else ""
        return f"[{self.code}] stage={self.stage}{suffix}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["name"] = "PipelineError"
        return data
