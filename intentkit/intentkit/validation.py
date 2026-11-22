"""输入与配置校验链路，输出结构化业务码并指明字段。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from .errors import BizError
from .models import IntentConfig, Ticket


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    field: str
    detail: dict

    def to_dict(self) -> dict:
        return {"code": self.code, "field": self.field, "detail": self.detail}


def validate_config(config: IntentConfig) -> List[ValidationIssue]:
    """校验意图配置，收集全部问题而非遇错即停。"""
    issues: List[ValidationIssue] = []
    controlled = set(config.controlled_labels)

    if config.default_fallback_label not in controlled:
        issues.append(
            ValidationIssue(
                "E_LABEL_NOT_CONTROLLED",
                "default_fallback_label",
                {"value": config.default_fallback_label},
            )
        )

    seen_labels = set()
    for idx, rule in enumerate(config.rules):
        base = f"rules[{idx}]"
        if rule.label in seen_labels:
            issues.append(
                ValidationIssue(
                    "E_LABEL_NOT_CONTROLLED",
                    f"{base}.label",
                    {"value": rule.label, "reason": "duplicate rule label"},
                )
            )
        seen_labels.add(rule.label)
        if rule.label not in controlled:
            issues.append(
                ValidationIssue(
                    "E_LABEL_NOT_CONTROLLED",
                    f"{base}.label",
                    {"value": rule.label},
                )
            )
        if not 0.0 <= rule.threshold <= 1.0:
            issues.append(
                ValidationIssue(
                    "E_THRESHOLD_RANGE",
                    f"{base}.threshold",
                    {"value": rule.threshold},
                )
            )
        if rule.batch_size <= 0:
            issues.append(
                ValidationIssue(
                    "E_RESOURCE_LIMIT",
                    f"{base}.batch_size",
                    {"value": rule.batch_size},
                )
            )
        for kw_idx, keyword in enumerate(rule.keywords):
            if keyword == "":
                issues.append(
                    ValidationIssue(
                        "E_EMPTY_KEYWORD",
                        f"{base}.keywords[{kw_idx}]",
                        {"label": rule.label},
                    )
                )
        if (
            rule.fallback_label is not None
            and rule.fallback_label not in controlled
        ):
            issues.append(
                ValidationIssue(
                    "E_LABEL_NOT_CONTROLLED",
                    f"{base}.fallback_label",
                    {"value": rule.fallback_label},
                )
            )
    return issues


def validate_tickets(tickets: Sequence[Ticket]) -> List[ValidationIssue]:
    """校验工单集合：重复 ID 与空查询文本。"""
    issues: List[ValidationIssue] = []
    seen = set()
    for idx, ticket in enumerate(tickets):
        if ticket.ticket_id in seen:
            issues.append(
                ValidationIssue(
                    "E_DUPLICATE_TICKET_ID",
                    f"tickets[{idx}].ticket_id",
                    {"value": ticket.ticket_id},
                )
            )
        seen.add(ticket.ticket_id)
        if ticket.query_text == "":
            issues.append(
                ValidationIssue(
                    "E_EMPTY_QUERY",
                    f"tickets[{idx}].query_text",
                    {"ticket_id": ticket.ticket_id},
                )
            )
    return issues


def assert_clean_config(config: IntentConfig) -> None:
    issues = validate_config(config)
    if issues:
        first = issues[0]
        raise BizError(first.code, first.field, first.detail)


def assert_clean_tickets(tickets: Sequence[Ticket]) -> None:
    issues = validate_tickets(tickets)
    if issues:
        first = issues[0]
        raise BizError(first.code, first.field, first.detail)


def validate_rule_dict(rule_dict: dict, controlled_labels: Sequence[str]) -> List[ValidationIssue]:
    """对未构建为 IntentRule 的原始字典做同口径校验，便于异常链路演示。"""
    issues: List[ValidationIssue] = []
    controlled = set(controlled_labels)
    label = rule_dict.get("label")
    if label not in controlled:
        issues.append(
            ValidationIssue(
                "E_LABEL_NOT_CONTROLLED",
                "label",
                {"value": label},
            )
        )
    threshold = rule_dict.get("threshold")
    if threshold is None or not 0.0 <= float(threshold) <= 1.0:
        issues.append(
            ValidationIssue(
                "E_THRESHOLD_RANGE",
                "threshold",
                {"value": threshold},
            )
        )
    for idx, keyword in enumerate(rule_dict.get("keywords", [])):
        if keyword == "":
            issues.append(
                ValidationIssue(
                    "E_EMPTY_KEYWORD",
                    f"keywords[{idx}]",
                    {"label": label},
                )
            )
    fallback = rule_dict.get("fallback_label")
    if fallback is not None and fallback not in controlled:
        issues.append(
            ValidationIssue(
                "E_LABEL_NOT_CONTROLLED",
                "fallback_label",
                {"value": fallback},
            )
        )
    return issues
