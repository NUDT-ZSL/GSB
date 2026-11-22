"""追溯链与重放校验。

每条判定记录携带缓存键、规则版本、门限版本；
按记录重放必须得到相同意图与码点偏移，否则返回 E_LINEAGE_BROKEN。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .cache import ContentStore
from .errors import BizError
from .matcher import make_cache_key
from .models import IntentConfig, Ticket


@dataclass
class ReplayReport:
    replayed_total: int = 0
    passed_total: int = 0
    failures: List[Dict[str, Any]] = field(default_factory=list)
    missing_lineage: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "replayed_total": self.replayed_total,
            "passed_total": self.passed_total,
            "all_passed": self.replayed_total == self.passed_total and not self.missing_lineage,
            "failures": list(self.failures),
            "missing_lineage": list(self.missing_lineage),
        }


def replay_one(
    ticket: Ticket,
    config: IntentConfig,
    recorded: Dict[str, Any],
    store: Optional[ContentStore] = None,
) -> Dict[str, Any]:
    """按单条记录的 lineage 重放并比对意图、偏移、置信度、缓存键。"""
    path = recorded["path"]
    expected_key = recorded["cache_key"]
    actual_key = make_cache_key(ticket, config, path)

    if actual_key != expected_key:
        raise BizError(
            "E_LINEAGE_BROKEN",
            f"tickets[{ticket.ticket_id}].cache_key",
            {
                "ticket_id": ticket.ticket_id,
                "expected_key": expected_key,
                "actual_key": actual_key,
            },
        )

    if store is not None and not store.has(expected_key):
        raise BizError(
            "E_LINEAGE_BROKEN",
            f"tickets[{ticket.ticket_id}].cache_artifact",
            {"ticket_id": ticket.ticket_id, "cache_key": expected_key},
        )

    if recorded.get("rule_version") != config.rule_version:
        raise BizError(
            "E_LINEAGE_BROKEN",
            f"tickets[{ticket.ticket_id}].rule_version",
            {
                "recorded": recorded.get("rule_version"),
                "current": config.rule_version,
            },
        )
    if recorded.get("threshold_version") != config.threshold_version:
        raise BizError(
            "E_LINEAGE_BROKEN",
            f"tickets[{ticket.ticket_id}].threshold_version",
            {
                "recorded": recorded.get("threshold_version"),
                "current": config.threshold_version,
            },
        )

    from .matcher import match_approximate, match_exact

    actual = (
        match_exact(ticket, config)
        if path == "exact"
        else match_approximate(ticket, config)
    )
    mismatches: List[str] = []
    if actual.intent_label != recorded["intent_label"]:
        mismatches.append("intent_label")
    if [hit.to_dict() for hit in actual.hits] != recorded["hits"]:
        mismatches.append("hits_offsets")
    if round(actual.confidence, 6) != round(float(recorded["confidence"]), 6):
        mismatches.append("confidence")
    if mismatches:
        raise BizError(
            "E_LINEAGE_BROKEN",
            f"tickets[{ticket.ticket_id}]",
            {
                "ticket_id": ticket.ticket_id,
                "mismatch_fields": mismatches,
                "recorded": recorded,
                "actual": actual.to_dict(),
            },
        )
    return actual.to_dict()


def replay_all(
    tickets: Sequence[Ticket],
    config: IntentConfig,
    records: Sequence[Dict[str, Any]],
    store: Optional[ContentStore] = None,
    raise_on_failure: bool = False,
) -> ReplayReport:
    """批量重放，收集失败与缺失追溯链；默认返回报告而非立即抛错。"""
    ticket_by_id = {ticket.ticket_id: ticket for ticket in tickets}
    report = ReplayReport()
    for recorded in records:
        ticket_id = recorded["ticket_id"]
        report.replayed_total += 1
        ticket = ticket_by_id.get(ticket_id)
        if ticket is None or not _lineage_complete(recorded):
            report.missing_lineage.append(
                {
                    "ticket_id": ticket_id,
                    "reason": "ticket_or_lineage_missing",
                    "has_ticket": ticket is not None,
                }
            )
            continue
        try:
            replay_one(ticket, config, recorded, store=store)
            report.passed_total += 1
        except BizError as error:
            report.failures.append(error.to_dict())
            if raise_on_failure:
                raise
    return report


def _lineage_complete(recorded: Dict[str, Any]) -> bool:
    required = ("cache_key", "rule_version", "threshold_version", "path", "intent_label", "hits")
    return all(key in recorded for key in required)
