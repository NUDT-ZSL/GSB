from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import RuleSet, VerificationRequest


ALLOWED = "ALLOW"
REJECTED = "REJECT"

REASON_ALLOWED = "ALLOWED"
REASON_TICKET_NOT_WHITELISTED = "TICKET_NOT_WHITELISTED"
REASON_BEFORE_WINDOW = "BEFORE_ENTRY_WINDOW"
REASON_AFTER_WINDOW = "AFTER_ENTRY_WINDOW"
REASON_REENTRY = "REENTRY_NOT_ALLOWED"
REASON_CHILD_ADULT = "CHILD_REQUIRES_ADULT"

RULE_MODE_CURRENT = "current"
RULE_MODE_DEGRADED = "degraded"


@dataclass(frozen=True)
class VerificationDecision:
    decision: str
    reason_code: str
    decisive_field: str
    trace: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "decisive_field": self.decisive_field,
            "trace": self.trace,
        }


def _field_trace(rule: RuleSet, field_name: str, value: Any) -> dict[str, Any]:
    return {
        "field": field_name,
        "source": rule.sources[field_name],
        "value": value,
    }


def evaluate_rule(request: VerificationRequest, rule: RuleSet) -> VerificationDecision:
    checked = [
        _field_trace(rule, "ticket_whitelist", list(rule.ticket_whitelist)),
        _field_trace(rule, "entry_window.start", rule.window_start),
        _field_trace(rule, "entry_window.end", rule.window_end),
        _field_trace(rule, "child_policy.requires_adult", rule.child_requires_adult),
        _field_trace(rule, "reentry_policy", rule.reentry_policy),
    ]
    base_trace = {
        "event_id": rule.event_id,
        "rule_version": rule.version,
        "checked_fields": checked,
    }

    if request.ticket_type not in rule.ticket_whitelist:
        return VerificationDecision(
            REJECTED,
            REASON_TICKET_NOT_WHITELISTED,
            "ticket_whitelist",
            {
                **base_trace,
                "decisive": _field_trace(
                    rule, "ticket_whitelist", list(rule.ticket_whitelist)
                ),
            },
        )

    if request.at_time < rule.start_at:
        return VerificationDecision(
            REJECTED,
            REASON_BEFORE_WINDOW,
            "entry_window.start",
            {
                **base_trace,
                "decisive": _field_trace(
                    rule, "entry_window.start", rule.window_start
                ),
            },
        )

    if request.at_time > rule.end_at:
        return VerificationDecision(
            REJECTED,
            REASON_AFTER_WINDOW,
            "entry_window.end",
            {
                **base_trace,
                "decisive": _field_trace(rule, "entry_window.end", rule.window_end),
            },
        )

    if (
        request.is_child
        and rule.child_requires_adult
        and request.accompanied_adult_count < 1
    ):
        return VerificationDecision(
            REJECTED,
            REASON_CHILD_ADULT,
            "child_policy.requires_adult",
            {
                **base_trace,
                "decisive": _field_trace(
                    rule, "child_policy.requires_adult", True
                ),
            },
        )

    max_entries = rule.max_entries()
    if max_entries is not None and request.prior_entries >= max_entries:
        return VerificationDecision(
            REJECTED,
            REASON_REENTRY,
            "reentry_policy",
            {
                **base_trace,
                "decisive": _field_trace(rule, "reentry_policy", rule.reentry_policy),
            },
        )

    return VerificationDecision(
        ALLOWED,
        REASON_ALLOWED,
        "",
        {
            **base_trace,
            "decisive": None,
        },
    )


def build_decision_record(
    request: VerificationRequest,
    rule: RuleSet,
    *,
    state_at_snapshot: str,
    effective_versions: dict[str, str],
    target_versions: dict[str, str],
    snapshot_after_batch: int | None,
) -> dict[str, Any]:
    evaluation = evaluate_rule(request, rule)
    mode = (
        RULE_MODE_CURRENT
        if state_at_snapshot == "active" and effective_versions == target_versions
        else RULE_MODE_DEGRADED
    )
    return {
        "request_id": request.request_id,
        "gate_id": request.gate_id,
        "event_id": request.event_id,
        "snapshot_after_batch": snapshot_after_batch,
        "state_at_snapshot": state_at_snapshot,
        "rule_mode": mode,
        "decision": evaluation.decision,
        "reason_code": evaluation.reason_code,
        "rule_version": rule.version,
        "effective_versions": dict(sorted(effective_versions.items())),
        "target_versions": dict(sorted(target_versions.items())),
        "inputs": {
            "at": request.at,
            "ticket_type": request.ticket_type,
            "prior_entries": request.prior_entries,
            "is_child": request.is_child,
            "accompanied_adult_count": request.accompanied_adult_count,
        },
        "trace": evaluation.trace,
    }
