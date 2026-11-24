from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .masking import field_matches_path, mask_value
from .models import Event, EventResult, FieldDecision, UnifiedRule, UnifiedView
from .rules import active_rules_for_field, conflicting_rules_for_field, rule_ranking_key


def _suppression_reason(
    suppressed: UnifiedRule,
    winner: UnifiedRule,
) -> str:
    suppressed_threshold = (
        suppressed.threshold if suppressed.threshold is not None else -1
    )
    winner_threshold = winner.threshold if winner.threshold is not None else -1
    if suppressed_threshold < winner_threshold:
        return "lower_threshold"
    suppressed_generation = (
        suppressed.generation if suppressed.generation is not None else -1
    )
    winner_generation = winner.generation if winner.generation is not None else -1
    if suppressed_generation < winner_generation:
        return "lower_generation"
    suppressed_effective = (
        suppressed.effective_ms if suppressed.effective_ms is not None else -1
    )
    winner_effective = winner.effective_ms if winner.effective_ms is not None else -1
    if suppressed_effective < winner_effective:
        return "older_effective_time"
    if suppressed.rule_name > winner.rule_name:
        return "rule_name_tiebreak"
    return "unselected"


def _match_detail(
    rule: UnifiedRule,
    selected: bool,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    detail = {
        "rule_name": rule.rule_name,
        "generation": rule.generation,
        "effective_ms": rule.effective_ms,
        "field_path": rule.field_path,
        "mask": rule.mask,
        "threshold": rule.threshold,
        "selected": selected,
    }
    if reason:
        detail["suppressed_reason"] = reason
    return detail


def decide_field(
    event: Event,
    field_name: str,
    view: UnifiedView,
) -> FieldDecision:
    field_path = f"payload.{field_name}"
    matching_rules = active_rules_for_field(view, field_name)
    matching_rules.sort(key=rule_ranking_key)
    conflicting = conflicting_rules_for_field(view, field_name)
    codes: List[str] = []
    if conflicting:
        codes.append("E_RULE_CONFLICT")

    if matching_rules:
        winner = matching_rules[0]
        matches: List[Dict[str, Any]] = []
        suppressed: List[Dict[str, Any]] = []
        for index, rule in enumerate(matching_rules):
            if index == 0:
                matches.append(_match_detail(rule, True))
            else:
                reason = _suppression_reason(rule, winner)
                detail = _match_detail(rule, False, reason)
                matches.append(detail)
                suppressed.append(detail)
        if len(matching_rules) > 1:
            codes.append("I_SUPPRESSED_RULES")
        mask = winner.mask or "keep"
        masked = mask_value(mask, event.payload[field_name])
        decision = "kept" if mask == "keep" else "masked"
        return FieldDecision(
            field_name=field_name,
            field_path=field_path,
            masked_value=masked,
            decision=decision,
            basis="rule",
            winning_rule_name=winner.rule_name,
            winning_generation=winner.generation or 0,
            winning_threshold=winner.threshold,
            winning_mask=mask,
            matches=matches,
            suppressed=suppressed,
            business_codes=sorted(set(codes)),
        )

    default = view.defaults.get(event.category)
    default_path = None
    if default:
        default_path = next(
            (
                pattern
                for pattern in default.fields
                if field_matches_path(pattern, field_name)
            ),
            None,
        )
    if default and default_path is not None:
        masked = mask_value(default.mask, event.payload[field_name])
        decision = "default_kept" if default.mask == "keep" else "default_masked"
        codes.append("I_CATEGORY_DEFAULT")
        detail = {
            "rule_name": default.rule_name,
            "generation": default.generation,
            "effective_ms": None,
            "field_path": default_path,
            "mask": default.mask,
            "threshold": None,
            "selected": True,
            "basis": "category_default",
        }
        return FieldDecision(
            field_name=field_name,
            field_path=field_path,
            masked_value=masked,
            decision=decision,
            basis="category_default",
            winning_rule_name=default.rule_name,
            winning_generation=default.generation,
            winning_threshold=None,
            winning_mask=default.mask,
            matches=[detail],
            suppressed=[],
            business_codes=sorted(set(codes)),
        )

    return FieldDecision(
        field_name=field_name,
        field_path=field_path,
        masked_value=event.payload[field_name],
        decision="unchanged",
        basis="no_rule",
        winning_rule_name="<none>",
        winning_generation=0,
        winning_threshold=None,
        winning_mask="keep",
        matches=[],
        suppressed=[],
        business_codes=sorted(set(codes)),
    )


def decide_event(
    event: Event,
    view: UnifiedView,
    decision_watermark: int = 0,
) -> EventResult:
    fields: Dict[str, FieldDecision] = {}
    codes = set()
    for field_name in sorted(event.payload):
        decision = decide_field(event, field_name, view)
        fields[field_name] = decision
        codes.update(decision.business_codes)
    status = "partial" if any(code.startswith("E_") for code in codes) else "ok"
    return EventResult(
        event=event,
        fields=fields,
        status=status,
        business_codes=sorted(codes),
        decision_watermark=decision_watermark,
    )


def decide_events(
    events: List[Event],
    view: UnifiedView,
    decision_watermark: int = 0,
) -> Dict[Tuple[str, int], EventResult]:
    results: Dict[Tuple[str, int], EventResult] = {}
    for event in sorted(events, key=lambda item: item.sort_key):
        results[event.sort_key] = decide_event(
            event,
            view,
            decision_watermark=decision_watermark,
        )
    return results
