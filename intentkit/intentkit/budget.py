"""匹配比较次数与驻留内存的双重预算准入。

超限时分别返回 E_MATCH_BUDGET / E_RESOURCE_LIMIT；
降批只改变批次划分，不改变任何已判定工单的结论；
未准入工单不会出现在结论集合中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from .errors import BizError
from .matcher import KeywordEntry, build_keyword_entries
from .models import IntentConfig, Ticket
from .selector import (
    SelectionDecision,
    estimate_approx_comparisons,
    estimate_exact_comparisons,
    observe,
)


def estimate_keyword_table_bytes(entries: Sequence[KeywordEntry]) -> int:
    """估算驻留关键词表内存：每个码点按 4 字节（UCS-4 口径）加固定条目开销。"""
    per_entry_overhead = 64
    return sum(per_entry_overhead + entry.length * 4 for entry in entries)


@dataclass(frozen=True)
class BudgetLimits:
    max_total_comparisons: int
    max_resident_bytes: int


@dataclass(frozen=True)
class BudgetAdmission:
    admitted: bool
    error_code: str
    error_field: str
    resident_bytes: int
    estimated_comparisons: int
    limits: BudgetLimits
    admitted_ticket_total: int
    rejected_ticket_total: int
    downgrade_category: str
    batches: List[List[Ticket]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "admitted": self.admitted,
            "error_code": self.error_code,
            "error_field": self.error_field,
            "resident_bytes": self.resident_bytes,
            "estimated_comparisons": self.estimated_comparisons,
            "limits": {
                "max_total_comparisons": self.limits.max_total_comparisons,
                "max_resident_bytes": self.limits.max_resident_bytes,
            },
            "admitted_ticket_total": self.admitted_ticket_total,
            "rejected_ticket_total": self.rejected_ticket_total,
            "downgrade_category": self.downgrade_category,
            "batch_sizes": [len(batch) for batch in self.batches],
        }


def _batch_estimate(
    tickets: Sequence[Ticket], config: IntentConfig, decision: SelectionDecision
) -> int:
    obs = observe(tickets, config)
    if decision.path == "exact":
        return estimate_exact_comparisons(obs)
    return estimate_approx_comparisons(config, obs)


def admit(
    tickets: Sequence[Ticket],
    config: IntentConfig,
    decision: SelectionDecision,
    limits: BudgetLimits,
) -> BudgetAdmission:
    """按内存预算先做驻留校验，再按比较预算做准入与降批。"""
    entries = build_keyword_entries(config)
    resident_bytes = estimate_keyword_table_bytes(entries)
    if resident_bytes > limits.max_resident_bytes:
        return BudgetAdmission(
            admitted=False,
            error_code="E_RESOURCE_LIMIT",
            error_field="keyword_table_resident_bytes",
            resident_bytes=resident_bytes,
            estimated_comparisons=decision.estimated_exact_comparisons,
            limits=limits,
            admitted_ticket_total=0,
            rejected_ticket_total=len(tickets),
            downgrade_category="none",
            batches=[],
        )

    full_estimate = _batch_estimate(tickets, config, decision)
    if full_estimate <= limits.max_total_comparisons and not decision.streaming:
        return BudgetAdmission(
            admitted=True,
            error_code="",
            error_field="",
            resident_bytes=resident_bytes,
            estimated_comparisons=full_estimate,
            limits=limits,
            admitted_ticket_total=len(tickets),
            rejected_ticket_total=0,
            downgrade_category="none",
            batches=[list(tickets)],
        )

    # 比较预算紧张或规模要求流式：按决策批大小降批，逐批计入预算。
    # 非流式但整批超预算时，进一步按规则批大小切分以实现预算降批。
    batch_size = decision.batch_size
    if not decision.streaming and full_estimate > limits.max_total_comparisons:
        configured = min(rule.batch_size for rule in config.rules)
        batch_size = min(batch_size, configured)
    batches = [
        list(tickets[start:start + batch_size])
        for start in range(0, len(tickets), batch_size)
    ]
    admitted_batches: List[List[Ticket]] = []
    used_comparisons = 0
    downgrade_category = "batch_downgrade" if decision.streaming else "budget_downgrade"
    for batch in batches:
        batch_cost = _batch_estimate(batch, config, decision)
        if used_comparisons + batch_cost > limits.max_total_comparisons:
            break
        used_comparisons += batch_cost
        admitted_batches.append(batch)

    admitted_count = sum(len(batch) for batch in admitted_batches)
    rejected_count = len(tickets) - admitted_count
    if admitted_count == 0:
        return BudgetAdmission(
            admitted=False,
            error_code="E_MATCH_BUDGET",
            error_field="max_total_comparisons",
            resident_bytes=resident_bytes,
            estimated_comparisons=full_estimate,
            limits=limits,
            admitted_ticket_total=0,
            rejected_ticket_total=len(tickets),
            downgrade_category=downgrade_category,
            batches=[],
        )

    return BudgetAdmission(
        admitted=True,
        error_code="" if rejected_count == 0 else "E_MATCH_BUDGET",
        error_field="" if rejected_count == 0 else "max_total_comparisons",
        resident_bytes=resident_bytes,
        estimated_comparisons=used_comparisons,
        limits=limits,
        admitted_ticket_total=admitted_count,
        rejected_ticket_total=rejected_count,
        downgrade_category=downgrade_category,
        batches=admitted_batches,
    )


def require_full_admission(admission: BudgetAdmission) -> None:
    """对要求全量准入的场景抛出结构化业务异常。"""
    if not admission.admitted:
        raise BizError(
            admission.error_code,
            admission.error_field,
            {
                "resident_bytes": admission.resident_bytes,
                "estimated_comparisons": admission.estimated_comparisons,
                "limits": {
                    "max_total_comparisons": admission.limits.max_total_comparisons,
                    "max_resident_bytes": admission.limits.max_resident_bytes,
                },
            },
        )
