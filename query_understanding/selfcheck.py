"""Cross-stage consistency checks."""

from __future__ import annotations

from typing import Any

from .errors import E_INCONSISTENT, QUError
from .taxonomy import Taxonomy


def _issue(category: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "category": category,
        "message": message,
        **details,
    }


def collect_consistency_issues(
    result: dict[str, Any],
    taxonomy: Taxonomy,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    normalized = result.get("normalized_text")
    if not isinstance(normalized, str):
        return [_issue("normalized_text_missing", "normalized_text must be a string")]

    tokens = result.get("tokens")
    if not isinstance(tokens, list):
        return [_issue("tokens_missing", "tokens must be a list")]
    previous_end = -1
    occupied: list[tuple[int, int]] = []
    covered: set[int] = set()
    for index, token in enumerate(tokens):
        start = token.get("start")
        end = token.get("end")
        text = token.get("text")
        if not isinstance(start, int) or not isinstance(end, int):
            issues.append(_issue("token_span_type", "token offsets must be integers", token_index=index))
            continue
        if not (0 <= start < end <= len(normalized)):
            issues.append(
                _issue(
                    "token_span_out_of_bounds",
                    "token span is outside the normalized text",
                    token_index=index,
                    start=start,
                    end=end,
                    normalized_length=len(normalized),
                )
            )
            continue
        if start < previous_end:
            issues.append(
                _issue(
                    "token_overlap",
                    "tokens overlap or are out of order",
                    token_index=index,
                    start=start,
                    previous_end=previous_end,
                )
            )
        previous_end = end
        occupied.append((start, end))
        if normalized[start:end] != text:
            issues.append(
                _issue(
                    "token_text_mismatch",
                    "token text does not match normalized_text span",
                    token_index=index,
                    expected=normalized[start:end],
                    actual=text,
                )
            )
        covered.update(range(start, end))
    expected_covered = {
        index for index, char in enumerate(normalized) if not char.isspace()
    }
    if covered != expected_covered:
        issues.append(
            _issue(
                "token_coverage",
                "tokens do not cover exactly the non-whitespace code points",
                missing=sorted(expected_covered - covered)[:10],
                extra=sorted(covered - expected_covered)[:10],
            )
        )

    domain = result.get("domain")
    intent_class = result.get("intent_class")
    intent = taxonomy.intent(str(domain), str(intent_class))
    if intent is None:
        issues.append(
            _issue(
                "intent_taxonomy_mismatch",
                "selected intent is not in taxonomy",
                domain=domain,
                intent_class=intent_class,
            )
        )

    domain_ranking = result.get("domain_ranking")
    if isinstance(domain_ranking, list) and domain_ranking:
        if domain_ranking[0].get("domain") != domain:
            issues.append(
                _issue(
                    "domain_top1_mismatch",
                    "selected domain does not equal top-1 domain",
                    selected_domain=domain,
                    top1_domain=domain_ranking[0].get("domain"),
                )
            )

    top_k = result.get("top_k_intents")
    if not isinstance(top_k, list) or not top_k:
        issues.append(_issue("top_k_missing", "top_k_intents must be non-empty"))
    else:
        first = top_k[0]
        if first.get("domain") != domain or first.get("intent_class") != intent_class:
            issues.append(
                _issue(
                    "top1_intent_mismatch",
                    "selected intent does not equal top-1 intent",
                    selected={"domain": domain, "intent_class": intent_class},
                    top1={
                        "domain": first.get("domain"),
                        "intent_class": first.get("intent_class"),
                    },
                )
            )
        if abs(float(first.get("confidence", -1.0)) - float(result.get("intent_confidence", -2.0))) > 1e-6:
            issues.append(
                _issue(
                    "intent_confidence_mismatch",
                    "intent_confidence does not equal top-1 confidence",
                )
            )
        for row in top_k:
            key = (str(row.get("domain")), str(row.get("intent_class")))
            if taxonomy.intent(*key) is None:
                issues.append(
                    _issue(
                        "top_k_taxonomy_mismatch",
                        "top-k intent is not in taxonomy",
                        domain=key[0],
                        intent_class=key[1],
                    )
                )
            confidence = float(row.get("confidence", -1))
            if not 0.0 <= confidence <= 1.0:
                issues.append(
                    _issue(
                        "confidence_out_of_range",
                        "intent confidence must be in [0,1]",
                        confidence=confidence,
                    )
                )

    slots = result.get("slots")
    if not isinstance(slots, list):
        issues.append(_issue("slots_missing", "slots must be a list"))
        return issues
    allowed_slots = set(intent.slot_names) if intent is not None else set()
    slot_occupied: list[tuple[int, int]] = []
    for index, slot in enumerate(slots):
        start = slot.get("start")
        end = slot.get("end")
        name = slot.get("slot_name")
        if not isinstance(start, int) or not isinstance(end, int):
            issues.append(_issue("slot_span_type", "slot offsets must be integers", slot_index=index))
            continue
        if not (0 <= start < end <= len(normalized)):
            issues.append(
                _issue(
                    "slot_span_out_of_bounds",
                    "slot span is outside the normalized text",
                    slot_index=index,
                    start=start,
                    end=end,
                )
            )
            continue
        if normalized[start:end] != slot.get("text"):
            issues.append(
                _issue(
                    "slot_text_mismatch",
                    "slot text does not match normalized_text span",
                    slot_index=index,
                    expected=normalized[start:end],
                    actual=slot.get("text"),
                )
            )
        if intent is not None and name not in allowed_slots:
            issues.append(
                _issue(
                    "slot_schema_mismatch",
                    "slot is not allowed by selected intent schema",
                    slot_index=index,
                    slot_name=name,
                )
            )
        if any(start < other_end and other_start < end for other_start, other_end in slot_occupied):
            issues.append(
                _issue(
                    "slot_overlap",
                    "slot spans overlap illegally",
                    slot_index=index,
                )
            )
        slot_occupied.append((start, end))
        confidence = float(slot.get("confidence", -1))
        if not 0.0 <= confidence <= 1.0:
            issues.append(
                _issue(
                    "slot_confidence_out_of_range",
                    "slot confidence must be in [0,1]",
                    slot_index=index,
                )
            )
        if not isinstance(slot.get("normalized_value"), str):
            issues.append(
                _issue(
                    "slot_value_type",
                    "normalized_value must be a string",
                    slot_index=index,
                )
            )
    return issues


def self_check(result: dict[str, Any], taxonomy: Taxonomy, *, raise_on_error: bool = True) -> dict[str, Any]:
    issues = collect_consistency_issues(result, taxonomy)
    report = {
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
    }
    if issues and raise_on_error:
        first = issues[0]
        raise QUError(
            E_INCONSISTENT,
            "cross-stage consistency check failed",
            stage="selfcheck",
            category=first["category"],
            details=report,
        )
    return report

