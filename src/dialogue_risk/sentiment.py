"""Deterministic lexicon sentiment scoring with negation and degree modifiers."""

from __future__ import annotations

import re
from typing import Any

from .normalize import normalize_text
from .util import quantize

_BOUNDARY = re.compile(r"[,，。.!！?？;；:：、()（）\[\]【】{}<>《》\"'“”‘’]")


def _sorted_terms(terms: list[str] | dict[str, Any]) -> list[str]:
    return sorted(terms, key=lambda term: (-len(term), term))


def _non_overlapping_matches(text: str, terms: list[str]) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    index = 0
    while index < len(text):
        matched = None
        for term in terms:
            if text.startswith(term, index):
                matched = term
                break
        if matched is None:
            index += 1
            continue
        matches.append((index, index + len(matched), matched))
        index += len(matched)
    return matches


def _modifier_window(normalized: str, term_start: int, lookback: int) -> str:
    start = max(0, term_start - lookback)
    prefix = normalized[start:term_start]
    last_boundary = -1
    for match in _BOUNDARY.finditer(prefix):
        last_boundary = match.end()
    return prefix if last_boundary < 0 else prefix[last_boundary:]


def score_sentiment(
    normalized_text: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Score one normalized message.

    Terms are scanned left-to-right with longest-term-first selection.  The
    nearest degree adverb in the bounded modifier window multiplies the term.
    An odd number of non-overlapping negation matches toggles its sign.
    """

    sentiment = config["sentiment"]
    decimals = int(config["precision"]["decimals"])
    lexicon = {term: float(score) for term, score in sentiment["lexicon"].items()}
    terms = _sorted_terms(lexicon)
    negations = _sorted_terms(config["context"]["negations"])
    degrees = _sorted_terms(config["context"]["degree_adverbs"])
    degree_values = {
        term: float(value)
        for term, value in config["context"]["degree_adverbs"].items()
    }
    lookback = int(config["context"]["lookback_codepoints"])

    positive = 0.0
    negative = 0.0
    details: list[dict[str, Any]] = []
    for start, end, term in _non_overlapping_matches(normalized_text, terms):
        window = _modifier_window(normalized_text, start, lookback)
        negation_matches = _non_overlapping_matches(window, negations)
        negated = len(negation_matches) % 2 == 1
        degree_matches = _non_overlapping_matches(window, degrees)
        if degree_matches:
            chosen = max(
                degree_matches,
                key=lambda match: (match[1], len(match[2]), match[2]),
            )
            degree_term = chosen[2]
            degree = degree_values[degree_term]
        else:
            degree_term = None
            degree = 1.0

        raw = lexicon[term]
        adjusted = raw * degree
        if negated:
            adjusted = -adjusted
        if adjusted > 0:
            positive += adjusted
        elif adjusted < 0:
            negative += -adjusted
        details.append(
            {
                "term": term,
                "start": start,
                "end": end,
                "base_score": quantize(raw, decimals),
                "negated": negated,
                "degree_term": degree_term,
                "degree": quantize(degree, decimals),
                "adjusted_score": quantize(adjusted, decimals),
            }
        )

    positive = quantize(positive, decimals)
    negative = quantize(negative, decimals)
    neutral = quantize(
        float(sentiment["neutral_baseline"]),
        decimals,
    )
    category_weights = sentiment["category_weights"]
    scores = {
        "negative": quantize(negative * float(category_weights["negative"]), decimals),
        "neutral": quantize(neutral * float(category_weights["neutral"]), decimals),
        "positive": quantize(positive * float(category_weights["positive"]), decimals),
    }

    label = sentiment["label_order"][0]
    for candidate in sentiment["label_order"][1:]:
        if scores[candidate] > scores[label]:
            label = candidate

    scale = max(1.0, positive + negative + neutral)
    valence = quantize((positive - negative) / scale, decimals)
    return {
        "normalized_text": normalized_text,
        "sentiment_scores": scores,
        "sentiment_label": label,
        "sentiment_score": valence,
        "sentiment_terms": details,
    }


def normalize_sentiment_input(text: str) -> str:
    """Expose the exact normalization used by sentiment tests and diagnostics."""

    return normalize_text(text)

