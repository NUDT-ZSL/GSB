"""Risk word matching, span validation and adjacent-message merging."""

from __future__ import annotations

from typing import Any

from .errors import fail
from .util import quantize


def _terms_for_category(
    risk_words: dict[str, Any], category: str
) -> list[dict[str, Any]]:
    entries = [
        {"term": entry["term"], "severity": float(entry["severity"])}
        for entry in risk_words["terms"][category]
    ]
    return sorted(entries, key=lambda item: (-len(item["term"]), item["term"]))


def _non_overlapping_matches(
    text: str, terms: list[dict[str, Any]]
) -> list[tuple[int, int, dict[str, Any]]]:
    matches: list[tuple[int, int, dict[str, Any]]] = []
    index = 0
    while index < len(text):
        matched = None
        for entry in terms:
            if text.startswith(entry["term"], index):
                matched = entry
                break
        if matched is None:
            index += 1
            continue
        matches.append((index, index + len(matched["term"]), matched))
        index += len(matched["term"])
    return matches


def validate_risk_span(
    normalized_text: str,
    category: str,
    start: int,
    end: int,
    risk_words: dict[str, Any],
    term: str | None = None,
) -> None:
    """Validate a prospective hit.  All violations use E_RISK_SPAN."""

    categories = risk_words["categories"]
    if category not in categories:
        fail(
            "E_RISK_SPAN",
            "risk category is not in the controlled set",
            category=category,
            controlled_categories=categories,
        )
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
    ):
        fail(
            "E_RISK_SPAN",
            "risk span boundaries must be integers",
            category=category,
            start=start,
            end=end,
        )
    if start < 0 or end <= start or end > len(normalized_text):
        fail(
            "E_RISK_SPAN",
            "risk span is outside normalized text code-point range",
            category=category,
            start=start,
            end=end,
            normalized_length=len(normalized_text),
        )
    observed = normalized_text[start:end]
    if term is not None:
        allowed_terms = {
            entry["term"] for entry in risk_words["terms"].get(category, [])
        }
        if observed != term or term not in allowed_terms:
            fail(
                "E_RISK_SPAN",
                "risk span content does not match the controlled term",
                category=category,
                start=start,
                end=end,
                term=term,
                observed=observed,
            )


def scan_risk_hits(
    msg_no: int,
    normalized_text: str,
    risk_words: dict[str, Any],
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for category in risk_words["categories"]:
        terms = _terms_for_category(risk_words, category)
        for start, end, entry in _non_overlapping_matches(normalized_text, terms):
            validate_risk_span(
                normalized_text,
                category,
                start,
                end,
                risk_words,
                term=entry["term"],
            )
            hits.append(
                {
                    "msg_no": msg_no,
                    "category": category,
                    "start": start,
                    "end": end,
                    "term": entry["term"],
                    "severity": entry["severity"],
                }
            )
    hits.sort(key=lambda hit: (hit["start"], hit["end"], hit["category"], hit["term"]))
    return hits


def merge_risk_candidates(
    messages: list[dict[str, Any]],
    risk_words: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge hits across adjacent messages.

    Explicit rule: two candidate runs of the same category are merged only when
    both messages have at least one hit and the next msg_no is exactly
    previous msg_no + 1.  A message without a hit, or a larger msg_no gap,
    starts a new candidate.  Within one message, all hits of that category are
    part of the same candidate.
    """

    category_order = {category: index for index, category in enumerate(risk_words["categories"])}
    candidates: list[dict[str, Any]] = []
    active: dict[str, dict[str, Any]] = {}
    for message in sorted(messages, key=lambda item: int(item["msg_no"])):
        msg_no = int(message["msg_no"])
        hits_by_category: dict[str, list[dict[str, Any]]] = {}
        for hit in message["risk_hits"]:
            hits_by_category.setdefault(hit["category"], []).append(hit)
        hit_categories = set(hits_by_category)
        for category in sorted(active):
            if category not in hit_categories or msg_no != active[category]["msg_end"] + 1:
                active.pop(category)
        for category in sorted(hits_by_category, key=lambda item: category_order[item]):
            hits = sorted(
                hits_by_category[category],
                key=lambda hit: (hit["start"], hit["end"], hit["term"]),
            )
            spans = [
                {
                    "msg_no": msg_no,
                    "start": int(hit["start"]),
                    "end": int(hit["end"]),
                    "term": hit["term"],
                    "severity": float(hit["severity"]),
                }
                for hit in hits
            ]
            if category in active:
                candidate = active[category]
                candidate["msg_end"] = msg_no
                candidate["hit_count"] += len(spans)
                candidate["max_severity"] = max(
                    candidate["max_severity"], max(span["severity"] for span in spans)
                )
                candidate["spans"].extend(spans)
                continue
            candidate = {
                "category": category,
                "msg_start": msg_no,
                "msg_end": msg_no,
                "hit_count": len(spans),
                "max_severity": max(span["severity"] for span in spans),
                "spans": spans,
            }
            active[category] = candidate
            candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            item["msg_start"],
            category_order[item["category"]],
            item["msg_end"],
        )
    )
    return candidates


def message_risk_intensity(
    hits: list[dict[str, Any]],
    config: dict[str, Any],
) -> float:
    decimals = int(config["precision"]["decimals"])
    severity_by_category: dict[str, float] = {}
    for hit in hits:
        category = hit["category"]
        severity_by_category[category] = max(
            severity_by_category.get(category, 0.0), float(hit["severity"])
        )
    category_weights = config["aggregation"]["risk_category_weights"]
    total = sum(
        float(category_weights[category]) * severity
        for category, severity in sorted(severity_by_category.items())
    )
    return quantize(min(total, float(config["aggregation"]["risk_score_cap"])), decimals)


def validate_result_risk_hits(result: dict[str, Any], risk_words: dict[str, Any]) -> None:
    for message in result["messages"]:
        normalized_text = message["normalized_text"]
        for hit in message["risk_hits"]:
            validate_risk_span(
                normalized_text,
                hit.get("category"),
                hit.get("start"),
                hit.get("end"),
                risk_words,
                term=hit.get("term"),
            )

