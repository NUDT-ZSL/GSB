"""判定编排引擎：取舍 -> 预算准入 -> 分批执行 -> 双路径裁决。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .budget import BudgetAdmission, BudgetLimits, admit
from .consistency import adjudicate
from .errors import BizError
from .matcher import (
    KeywordEntry,
    MatchResult,
    build_keyword_entries,
    build_signature_buckets,
    match_approximate,
    match_exact,
)
from .models import IntentConfig, Ticket
from .selector import SelectionDecision, choose_path


@dataclass
class EngineOutput:
    selected_results: List[MatchResult]
    exact_results: List[MatchResult]
    approximate_results: List[MatchResult]
    decision: SelectionDecision
    admission: BudgetAdmission
    consistency_report: Dict[str, Any]
    batch_markers: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_path": self.decision.path,
            "decision": self.decision.to_dict(),
            "admission": self.admission.to_dict(),
            "batch_markers": [dict(item) for item in self.batch_markers],
            "selected_results": [item.to_dict() for item in self.selected_results],
            "exact_results": [item.to_dict() for item in self.exact_results],
            "approximate_results": [item.to_dict() for item in self.approximate_results],
            "consistency_report": self.consistency_report,
        }


def run_engine(
    tickets: Sequence[Ticket],
    config: IntentConfig,
    limits: BudgetLimits,
    force_skip: Optional[Tuple[str, str]] = None,
) -> EngineOutput:
    """全流程执行，双路径始终都跑以便裁决；准入只约束对外结论集合。"""
    decision = choose_path(tickets, config)
    admission = admit(tickets, config, decision, limits)
    if not admission.admitted:
        raise BizError(
            admission.error_code,
            admission.error_field,
            {
                "resident_bytes": admission.resident_bytes,
                "estimated_comparisons": admission.estimated_comparisons,
                "admitted_ticket_total": 0,
                "rejected_ticket_total": admission.rejected_ticket_total,
            },
        )

    entries: List[KeywordEntry] = build_keyword_entries(config)
    buckets = build_signature_buckets(entries)

    admitted_ids = {
        ticket.ticket_id for batch in admission.batches for ticket in batch
    }
    admitted_tickets = [ticket for ticket in tickets if ticket.ticket_id in admitted_ids]

    exact_results: List[MatchResult] = []
    approximate_results: List[MatchResult] = []
    batch_markers: List[Dict[str, Any]] = []

    for batch_index, batch in enumerate(admission.batches):
        for ticket in batch:
            exact_results.append(
                match_exact(ticket, config, entries=entries)
            )
            approximate_results.append(
                match_approximate(
                    ticket,
                    config,
                    entries=entries,
                    buckets=buckets,
                    force_skip=force_skip,
                )
            )
        batch_markers.append(
            {
                "batch_index": batch_index,
                "batch_size": len(batch),
                "downgrade_category": admission.downgrade_category,
                "first_ticket_id": batch[0].ticket_id,
                "last_ticket_id": batch[-1].ticket_id,
            }
        )

    consistency_report = adjudicate(
        exact_results,
        approximate_results,
        raise_on_divergence=force_skip is not None,
    )

    selected_results = (
        exact_results if decision.path == "exact" else approximate_results
    )
    _assert_full_coverage(admitted_tickets, selected_results, config)
    return EngineOutput(
        selected_results=selected_results,
        exact_results=exact_results,
        approximate_results=approximate_results,
        decision=decision,
        admission=admission,
        consistency_report=consistency_report,
        batch_markers=batch_markers,
    )


def _assert_full_coverage(
    admitted_tickets: Sequence[Ticket],
    results: Sequence[MatchResult],
    config: IntentConfig,
) -> None:
    result_ids = {result.ticket_id for result in results}
    expected_ids = {ticket.ticket_id for ticket in admitted_tickets}
    if result_ids != expected_ids:
        raise RuntimeError(
            "coverage broken: "
            f"missing={sorted(expected_ids - result_ids)}, "
            f"unexpected={sorted(result_ids - expected_ids)}"
        )
    enabled = {rule.label for rule in config.rules}
    # 启用规则全覆盖指关键词表包含全部启用意图；是否命中取决于文本，
    # 这里校验的是参与匹配的关键词条目覆盖全部启用意图。
    entries = build_keyword_entries(config)
    entry_labels = {entry.label for entry in entries}
    if entry_labels != enabled:
        raise RuntimeError(
            "intent coverage broken: "
            f"missing_rules={sorted(enabled - entry_labels)}"
        )
