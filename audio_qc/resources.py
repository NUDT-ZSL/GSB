"""Configurable memory, cache, and parallel-chunk budgets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .codes import BusinessCode
from .errors import QCError


@dataclass(frozen=True)
class ResourceBudget:
    max_resident_blocks: int = 2_048
    max_resident_bytes: int = 128 * 1024 * 1024
    max_cache_entries: int = 20_000
    max_parallel_chunks: int = 4
    requested_workers: int = 1
    estimated_block_bytes: int = 16 * 1024

    def validate(self) -> None:
        if self.max_resident_blocks <= 0:
            raise ValueError("max_resident_blocks must be positive")
        if self.max_resident_bytes <= 0:
            raise ValueError("max_resident_bytes must be positive")
        if self.max_cache_entries <= 0:
            raise ValueError("max_cache_entries must be positive")
        if self.max_parallel_chunks <= 0:
            raise ValueError("max_parallel_chunks must be positive")
        if self.requested_workers < 1:
            raise ValueError("requested_workers must be at least 1")


@dataclass(frozen=True)
class BudgetPlan:
    mode: str
    workers: int
    code: str
    reasons: list[str]
    projected_resident_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "workers": self.workers,
            "business_code": self.code,
            "reasons": self.reasons,
            "projected_resident_bytes": self.projected_resident_bytes,
        }


class ResourceGovernor:
    def __init__(self, budget: ResourceBudget | None = None):
        self.budget = budget or ResourceBudget()
        self.budget.validate()

    def check_resident(self, block_count: int) -> None:
        projected = block_count * self.budget.estimated_block_bytes
        if block_count > self.budget.max_resident_blocks or projected > self.budget.max_resident_bytes:
            raise QCError(
                BusinessCode.E_MEMORY_BUDGET,
                "resident envelope statistics exceed the configured memory budget",
                {
                    "block_count": block_count,
                    "max_resident_blocks": self.budget.max_resident_blocks,
                    "projected_bytes": projected,
                    "max_resident_bytes": self.budget.max_resident_bytes,
                },
            )

    def check_parallel(self, requested_workers: int | None = None) -> None:
        workers = requested_workers or self.budget.requested_workers
        if workers > self.budget.max_parallel_chunks:
            raise QCError(
                BusinessCode.E_RESOURCE_LIMIT,
                "requested parallel chunks exceed the configured limit",
                {"requested_workers": workers, "max_parallel_chunks": self.budget.max_parallel_chunks},
            )

    def plan(self, block_count: int, requested_workers: int | None = None) -> BudgetPlan:
        workers = requested_workers or self.budget.requested_workers
        projected = block_count * self.budget.estimated_block_bytes
        memory_reasons: list[str] = []
        if block_count > self.budget.max_resident_blocks:
            memory_reasons.append("max_resident_blocks")
        if projected > self.budget.max_resident_bytes:
            memory_reasons.append("max_resident_bytes")

        resource_reasons: list[str] = []
        if workers > self.budget.max_parallel_chunks:
            resource_reasons.append("max_parallel_chunks")
        if block_count > self.budget.max_cache_entries:
            resource_reasons.append("max_cache_entries")

        if memory_reasons:
            return BudgetPlan(
                mode="serial_streaming",
                workers=1,
                code=BusinessCode.E_MEMORY_BUDGET,
                reasons=memory_reasons,
                projected_resident_bytes=projected,
            )
        if resource_reasons:
            return BudgetPlan(
                mode="serial",
                workers=1,
                code=BusinessCode.E_RESOURCE_LIMIT,
                reasons=resource_reasons,
                projected_resident_bytes=projected,
            )
        effective_workers = min(workers, self.budget.max_parallel_chunks)
        return BudgetPlan(
            mode="parallel" if effective_workers > 1 else "serial",
            workers=effective_workers,
            code=BusinessCode.OK,
            reasons=[],
            projected_resident_bytes=projected,
        )
