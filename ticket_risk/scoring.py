"""Pure, deterministic normalization, scoring, risk, and fusion stages."""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Iterable

import numpy as np

from .loader import RISK_LEVELS, RISK_ORDER, Sample
from .util import round_float, sha256_obj


ALGORITHM_VERSION = 1
_SPACE_RE = re.compile(r"\s+")


def normalize_text(raw_text: str) -> str:
    """NFKC fold, case fold, strip punctuation, collapse whitespace."""

    folded = unicodedata.normalize("NFKC", raw_text).casefold()
    chars: list[str] = []
    for char in folded:
        if unicodedata.category(char).startswith("P"):
            chars.append(" ")
        elif char.isspace():
            chars.append(" ")
        else:
            chars.append(char)
    return _SPACE_RE.sub(" ", "".join(chars)).strip()


def _scan_terms(text: str, words: Iterable[str]) -> list[dict[str, Any]]:
    """Find deterministic, non-overlapping dictionary occurrences.

    Candidate order is ``(start, -length, word)`` so a longer word wins when
    two dictionary entries start at the same character.  Accepted occurrences
    are finally emitted in ``(start, word)`` order.
    """

    candidates: list[tuple[int, int, str]] = []
    normalized_word_map: dict[str, str] = {}
    for original in sorted(set(words)):
        normalized = normalize_text(original)
        if not normalized:
            continue
        normalized_word_map[normalized] = original
        start = 0
        while True:
            found = text.find(normalized, start)
            if found < 0:
                break
            candidates.append((found, found + len(normalized), normalized))
            start = found + len(normalized)

    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    occupied = [False] * len(text)
    accepted: list[tuple[int, int, str]] = []
    for start, end, normalized in candidates:
        if any(occupied[start:end]):
            continue
        for index in range(start, end):
            occupied[index] = True
        accepted.append((start, end, normalized))
    accepted.sort(key=lambda item: (item[0], item[2]))
    return [
        {
            "start": start,
            "end": end,
            "word": normalized_word_map[normalized],
            "normalized_word": normalized,
        }
        for start, end, normalized in accepted
    ]


def find_dictionary_matches(text: str, words: Iterable[str]) -> list[dict[str, Any]]:
    """Public deterministic matcher used by scoring and the reference index."""

    return _scan_terms(text, words)


def _stage_cache_key(
    stage: str,
    upstream_fingerprint: str,
    params: dict[str, Any],
    resource_projection: dict[str, Any],
) -> str:
    return sha256_obj(
        {
            "stage": stage,
            "algorithm_version": ALGORITHM_VERSION,
            "upstream": upstream_fingerprint,
            "params": params,
            "resource_projection": resource_projection,
        }
    )


def normalize_stage(sample: Sample, rules: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_text(sample.text)
    upstream = sha256_obj({"text": sample.text})
    resource_projection = {
        "schema_version": rules.get("schema_version", 1),
        "normalization_contract": "nfkc-casefold-punctuation-space-v1",
    }
    key = _stage_cache_key(
        "normalize",
        upstream,
        {"normalizer_version": ALGORITHM_VERSION},
        resource_projection,
    )
    return {
        "cache_key": key,
        "upstream_fingerprint": upstream,
        "normalized_text": normalized,
    }


def _degree_matches(
    prefix: str,
    degree_adverbs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    matches = _scan_terms(prefix, degree_adverbs)
    matches.sort(key=lambda item: (-item["end"], item["normalized_word"]))
    return matches


def _negation_matches(prefix: str, negation_words: list[str]) -> list[dict[str, Any]]:
    return _scan_terms(prefix, negation_words)


def sentiment_stage(
    sample: Sample,
    normalized_artifact: dict[str, Any],
    rules: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    text = normalized_artifact["normalized_text"]
    lexicon = rules["sentiment_lexicon"]
    matches = _scan_terms(text, lexicon)
    scale = float(config["lexicon_weight_scale"])
    decay = float(config["position_decay"])
    window = int(config["negation_window"])

    contributions: list[dict[str, Any]] = []
    used_lexicon: set[str] = set()
    used_degree: set[str] = set()
    used_negations: set[str] = set()
    denominator = max(1, len(text))
    for match in matches:
        word = match["word"]
        used_lexicon.add(word)
        lex_weight = float(lexicon[word]["weight"])
        prefix_start = max(0, match["start"] - window)
        prefix = text[prefix_start : match["start"]]
        degree_matches = _degree_matches(prefix, rules["degree_adverbs"])
        if degree_matches:
            degree_word = degree_matches[0]["word"]
            degree_factor = float(rules["degree_adverbs"][degree_word]["factor"])
            used_degree.add(degree_word)
        else:
            degree_word = None
            degree_factor = 1.0
        negations = _negation_matches(prefix, rules["negation_words"])
        # Avoid substring false positives such as "别" inside the degree word
        # "特别".  A negation only counts when it is not contained in a degree
        # adverb occurrence in the same prefix.
        negations = [
            negation
            for negation in negations
            if not any(
                degree["start"] <= negation["start"]
                and negation["end"] <= degree["end"]
                for degree in degree_matches
            )
        ]
        for item in negations:
            used_negations.add(item["word"])
        negated = len(negations) % 2 == 1
        position_ratio = match["start"] / denominator
        position_factor = max(0.0, 1.0 - decay * position_ratio)
        raw = lex_weight * scale * position_factor * degree_factor
        if negated:
            raw = -raw
        polarity = "positive" if raw > 0 else "negative" if raw < 0 else "neutral"
        contributions.append(
            {
                "word": word,
                "position": match["start"],
                "end": match["end"],
                "lexicon_weight": round_float(lex_weight),
                "position_ratio": round_float(position_ratio),
                "position_factor": round_float(position_factor),
                "degree_word": degree_word,
                "degree_factor": round_float(degree_factor),
                "negation_words": [item["word"] for item in negations],
                "negated": negated,
                "polarity": polarity,
                "raw_contribution": raw,
            }
        )

    contributions.sort(key=lambda item: (item["position"], item["word"]))
    raw_total = 0.0
    for item in contributions:
        raw_total += float(item["raw_contribution"])
    normalized_score = float(np.tanh(np.float64(raw_total)))
    if abs(normalized_score) < 1e-15:
        normalized_score = 0.0

    for item in contributions:
        if raw_total == 0:
            normalized_contribution = 0.0
        else:
            normalized_contribution = (
                float(item["raw_contribution"]) * normalized_score / raw_total
            )
        item["raw_contribution"] = round_float(float(item["raw_contribution"]))
        item["normalized_contribution"] = round_float(normalized_contribution)

    dominant_word = None
    if contributions:
        dominant = sorted(
            contributions,
            key=lambda item: (-abs(float(item["raw_contribution"])), item["word"]),
        )[0]
        dominant_word = dominant["word"]

    polarity = "positive" if normalized_score > 1e-12 else "negative" if normalized_score < -1e-12 else "neutral"
    resource_projection = {
        "lexicon": [
            {"word": word, "weight": float(lexicon[word]["weight"])}
            for word in sorted(used_lexicon)
        ],
        "degree_adverbs": [
            {"word": word, "factor": float(rules["degree_adverbs"][word]["factor"])}
            for word in sorted(used_degree)
        ],
        "negation_words": sorted(used_negations),
    }
    params = {"position_decay": decay, "negation_window": window}
    if contributions:
        params["lexicon_weight_scale"] = scale
    key = _stage_cache_key(
        "sentiment",
        normalized_artifact["cache_key"],
        params,
        resource_projection,
    )
    return {
        "cache_key": key,
        "raw_score": round_float(raw_total),
        "normalized_score": round_float(normalized_score),
        "polarity": polarity,
        "dominant_word": dominant_word,
        "contributions": contributions,
    }


def _risk_level_for_score(score: float, thresholds: dict[str, float]) -> str:
    level = "一般"
    for candidate in RISK_LEVELS:
        if score + 1e-15 >= float(thresholds[candidate]):
            level = candidate
    return level


def risk_stage(
    sample: Sample,
    normalized_artifact: dict[str, Any],
    rules: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    text = normalized_artifact["normalized_text"]
    keywords = rules["risk_keywords"]
    level_map = rules["risk_level_map"]
    matches = _scan_terms(text, keywords)
    scale = float(config["risk_weight_scale"])
    decay = float(config["position_decay"])
    thresholds = {
        level: float(config["risk_level_thresholds"][level]) for level in RISK_LEVELS
    }
    denominator = max(1, len(text))
    contributions: list[dict[str, Any]] = []
    used_keywords: set[str] = set()
    used_levels: set[str] = set()
    for match in matches:
        keyword = match["word"]
        used_keywords.add(keyword)
        entry = keywords[keyword]
        level = entry["level"]
        used_levels.add(level)
        position_ratio = match["start"] / denominator
        position_factor = max(0.0, 1.0 - decay * position_ratio)
        keyword_weight = float(entry["weight"])
        level_weight = float(level_map[level]["weight"])
        raw = keyword_weight * level_weight * scale * position_factor
        contributions.append(
            {
                "keyword": keyword,
                "risk_level": level,
                "position": match["start"],
                "end": match["end"],
                "keyword_weight": round_float(keyword_weight),
                "level_map_weight": round_float(level_weight),
                "position_ratio": round_float(position_ratio),
                "position_factor": round_float(position_factor),
                "raw_contribution": raw,
            }
        )

    contributions.sort(key=lambda item: (item["position"], item["keyword"]))
    raw_total = 0.0
    for item in contributions:
        raw_total += float(item["raw_contribution"])
    risk_score = 1.0 - math.exp(-max(0.0, raw_total))
    if abs(risk_score) < 1e-15:
        risk_score = 0.0
    for item in contributions:
        normalized_contribution = (
            float(item["raw_contribution"]) * risk_score / raw_total
            if raw_total > 0
            else 0.0
        )
        item["raw_contribution"] = round_float(float(item["raw_contribution"]))
        item["normalized_contribution"] = round_float(normalized_contribution)
    base_level = _risk_level_for_score(risk_score, thresholds)

    resource_projection = {
        "keywords": [
            {
                "keyword": keyword,
                "level": keywords[keyword]["level"],
                "weight": float(keywords[keyword]["weight"]),
            }
            for keyword in sorted(used_keywords)
        ],
        "level_map": [
            {
                "level": level,
                "weight": float(level_map[level]["weight"]),
            }
            for level in sorted(used_levels)
        ],
    }
    params = {
        "position_decay": decay,
        "risk_level_thresholds": thresholds,
    }
    if contributions:
        params["risk_weight_scale"] = scale
    key = _stage_cache_key(
        "risk",
        normalized_artifact["cache_key"],
        params,
        resource_projection,
    )
    return {
        "cache_key": key,
        "raw_score": round_float(raw_total),
        "risk_score": round_float(risk_score),
        "base_risk_level": base_level,
        "contributions": contributions,
    }


def _stable_softmax(values: list[float]) -> list[float]:
    array = np.asarray(values, dtype=np.float64)
    shifted = array - np.max(array)
    exponentials = np.exp(shifted)
    probabilities = exponentials / np.sum(exponentials)
    return [float(item) for item in probabilities]


def fusion_stage(
    sample: Sample,
    sentiment_artifact: dict[str, Any],
    risk_artifact: dict[str, Any],
    rules: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    sentiment_score = float(sentiment_artifact["normalized_score"])
    risk_score = float(risk_artifact["risk_score"])
    sentiment_risk_signal = (1.0 - sentiment_score) / 2.0
    temperature = float(config["fusion_temperature"])
    logits = [risk_score / temperature, sentiment_risk_signal / temperature]
    weights = _stable_softmax(logits)
    weighted_contributions = {
        "risk": weights[0] * risk_score,
        "sentiment": weights[1] * sentiment_risk_signal,
    }
    confidence = weighted_contributions["risk"] + weighted_contributions["sentiment"]
    if abs(confidence) < 1e-15:
        confidence = 0.0

    conflict_lookup = {
        (item["sentiment"], item["risk_level"]): item for item in rules["conflict_rules"]
    }
    pair = (sentiment_artifact["polarity"], risk_artifact["base_risk_level"])
    conflict_rule = conflict_lookup.get(pair)
    if conflict_rule is None:
        final_level = risk_artifact["base_risk_level"]
        conflict = {"matched": False, "pair": list(pair)}
        rationale = {
            "kind": "threshold",
            "reason": "no_conflict_rule",
            "base_risk_level": risk_artifact["base_risk_level"],
        }
    else:
        final_level = conflict_rule["resolved_level"]
        conflict = {
            "matched": True,
            "pair": list(pair),
            "winner": conflict_rule["winner"],
            "reason": conflict_rule["reason"],
            "resolved_level": conflict_rule["resolved_level"],
        }
        rationale = {
            "kind": "conflict_rule",
            "reason": conflict_rule["reason"],
            "winner": conflict_rule["winner"],
            "base_risk_level": risk_artifact["base_risk_level"],
            "resolved_level": final_level,
        }

    resource_projection = {
        "conflict_rules": rules["conflict_rules"],
        "risk_level_order": list(RISK_LEVELS),
    }
    key = _stage_cache_key(
        "fusion",
        sha256_obj(
            {
                "sentiment": sentiment_artifact["cache_key"],
                "risk": risk_artifact["cache_key"],
            }
        ),
        {"fusion_temperature": temperature},
        resource_projection,
    )
    return {
        "cache_key": key,
        "final_risk_level": final_level,
        "confidence": round_float(confidence),
        "sentiment_polarity": sentiment_artifact["polarity"],
        "sentiment_score": round_float(sentiment_score),
        "risk_score": round_float(risk_score),
        "sentiment_risk_signal": round_float(sentiment_risk_signal),
        "temperature": temperature,
        "logits": [round_float(item) for item in logits],
        "softmax_weights": {
            "risk": round_float(weights[0]),
            "sentiment": round_float(weights[1]),
        },
        "weighted_contributions": {
            "risk": round_float(weighted_contributions["risk"]),
            "sentiment": round_float(weighted_contributions["sentiment"]),
        },
        "conflict": conflict,
        "rationale": rationale,
    }


def result_from_artifacts(sample: Sample, artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sentiment = artifacts["sentiment"]
    risk = artifacts["risk"]
    fusion = artifacts["fusion"]
    return {
        "sample_id": sample.sample_id,
        "text": sample.text,
        "normalized_text": artifacts["normalize"]["normalized_text"],
        "channel": sample.channel,
        "sentiment": {
            "score": sentiment["normalized_score"],
            "polarity": sentiment["polarity"],
            "dominant_word": sentiment["dominant_word"],
            "contributions": sentiment["contributions"],
        },
        "risk": {
            "score": risk["risk_score"],
            "base_level": risk["base_risk_level"],
            "contributions": risk["contributions"],
        },
        "fusion": {
            "confidence": fusion["confidence"],
            "risk_signal": fusion["risk_score"],
            "sentiment_risk_signal": fusion["sentiment_risk_signal"],
            "temperature": fusion["temperature"],
            "logits": fusion["logits"],
            "softmax_weights": fusion["softmax_weights"],
            "weighted_contributions": fusion["weighted_contributions"],
            "conflict": fusion["conflict"],
        },
        "risk_level": fusion["final_risk_level"],
        "confidence": fusion["confidence"],
        "rationale": fusion["rationale"],
    }


def rank_sample_ids(results: list[dict[str, Any]]) -> list[str]:
    ranked = sorted(
        results,
        key=lambda item: (
            -float(item["confidence"]),
            -RISK_ORDER[item["risk_level"]],
            item["sample_id"],
        ),
    )
    return [item["sample_id"] for item in ranked]


def risk_distribution(results: list[dict[str, Any]]) -> dict[str, int]:
    distribution = {level: 0 for level in RISK_LEVELS}
    for result in results:
        distribution[result["risk_level"]] += 1
    return distribution
