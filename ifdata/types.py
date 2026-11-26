"""Typed containers used by the scheduling and snapshot layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


STAGES: tuple[tuple[int, str, str], ...] = (
    (1, "ingest", "样本摄入与校验"),
    (2, "template", "模板规范化"),
    (3, "quality", "质量清洗打分"),
    (4, "dedup", "近重复检测去重"),
    (5, "sampling", "配比采样"),
    (6, "export", "数据集导出"),
)

STAGE_NAMES = tuple(name for _, name, _ in STAGES)
STAGE_INDEX = {name: index for index, name, _ in STAGES}


@dataclass(frozen=True)
class BatchEstimate:
    batch_id: str
    sample_count: int
    comparisons: int
    memory_bytes: int
    requested_workers: int = 1


@dataclass(frozen=True)
class BudgetLimits:
    max_workers: int
    compare_budget: int
    memory_limit_bytes: int
    error_priority: tuple[str, ...] = (
        "E_CONCURRENCY_LIMIT",
        "E_COMPARE_BUDGET",
        "E_MEMORY_LIMIT",
    )


@dataclass
class AdmissionPlan:
    admitted: list[BatchEstimate] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    comparisons_used: int = 0
    peak_memory_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.rejected

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted_batch_ids": [batch.batch_id for batch in self.admitted],
            "rejected": self.rejected,
            "comparisons_used": self.comparisons_used,
            "peak_memory_bytes": self.peak_memory_bytes,
        }


@dataclass
class StageResult:
    stage: str
    stage_index: int
    completed_sample_ids: list[str]
    payload: dict[str, Any]

