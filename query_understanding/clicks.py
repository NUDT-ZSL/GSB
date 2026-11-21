"""Deterministic pseudo-relevance feedback and intent reranking."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .taxonomy import Taxonomy


@dataclass(frozen=True)
class ClickPolicy:
    feedback_rank_max: int = 2
    feedback_min_dwell_ms: int = 300
    eval_rank_min: int = 3
    eval_rank_max: int = 5
    click_weight: float = 0.55
    ambiguity_margin: float = 0.08
    softmax_temperature: float = 0.18
    top_k: int = 3

    @classmethod
    def from_dict(cls, row: dict[str, Any] | None) -> "ClickPolicy":
        row = row or {}
        return cls(
            feedback_rank_max=int(row.get("feedback_rank_max", 2)),
            feedback_min_dwell_ms=int(row.get("feedback_min_dwell_ms", 300)),
            eval_rank_min=int(row.get("eval_rank_min", 3)),
            eval_rank_max=int(row.get("eval_rank_max", 5)),
            click_weight=float(row.get("click_weight", 0.55)),
            ambiguity_margin=float(row.get("ambiguity_margin", 0.08)),
            softmax_temperature=float(row.get("softmax_temperature", 0.18)),
            top_k=int(row.get("top_k", 3)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "feedback_rank_max": self.feedback_rank_max,
            "feedback_min_dwell_ms": self.feedback_min_dwell_ms,
            "eval_rank_min": self.eval_rank_min,
            "eval_rank_max": self.eval_rank_max,
            "click_weight": self.click_weight,
            "ambiguity_margin": self.ambiguity_margin,
            "softmax_temperature": self.softmax_temperature,
            "top_k": self.top_k,
        }


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def feedback_clicks(
    raw_clicks: list[dict[str, Any]], policy: ClickPolicy
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    used: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for click in sorted(
        raw_clicks,
        key=lambda row: (
            int(row.get("rank", 10**9)),
            str(row.get("doc_id", "")),
            str(row.get("intent_class", "")),
        ),
    ):
        rank = int(click.get("rank", 10**9))
        dwell = int(click.get("dwell_ms", 0))
        reason = None
        if rank > policy.feedback_rank_max:
            reason = "outside_feedback_rank_window"
        elif dwell < policy.feedback_min_dwell_ms:
            reason = "below_feedback_dwell_floor"
        if reason is None:
            used.append(click)
        else:
            excluded.append(
                {
                    "doc_id": click.get("doc_id"),
                    "rank": rank,
                    "reason": reason,
                }
            )
    return used, excluded


def eval_clicks(
    raw_clicks: list[dict[str, Any]], policy: ClickPolicy
) -> list[dict[str, Any]]:
    rows = []
    for click in raw_clicks:
        rank = int(click.get("rank", 10**9))
        if policy.eval_rank_min <= rank <= policy.eval_rank_max:
            rows.append(click)
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("rank", 10**9)),
            str(row.get("doc_id", "")),
            str(row.get("intent_class", "")),
        ),
    )


def _normalize_scores(scores: dict[tuple[str, str], float]) -> dict[tuple[str, str], float]:
    if not scores:
        return {}
    maximum = max(scores.values())
    if maximum <= 0:
        return {key: 0.0 for key in scores}
    return {key: value / maximum for key, value in scores.items()}


def _click_scores(
    used_clicks: list[dict[str, Any]],
    policy: ClickPolicy,
) -> dict[tuple[str, str], float]:
    scores: dict[tuple[str, str], float] = {}
    for click in used_clicks:
        domain = str(click.get("domain", ""))
        intent_class = str(click.get("intent_class", ""))
        rank = max(1, int(click.get("rank", 1)))
        dwell = max(0, int(click.get("dwell_ms", 0)))
        dwell_factor = _clip(dwell / 2000.0, 0.0, 1.0)
        score = dwell_factor / rank
        key = (domain, intent_class)
        scores[key] = scores.get(key, 0.0) + score
    return _normalize_scores(scores)


def rerank_intents(
    lexical_scores: dict[tuple[str, str], float],
    raw_clicks: list[dict[str, Any]],
    taxonomy: Taxonomy,
    policy: ClickPolicy,
) -> dict[str, Any]:
    used, excluded = feedback_clicks(raw_clicks, policy)
    valid_lexical = {
        key: value
        for key, value in lexical_scores.items()
        if taxonomy.intent(*key) is not None
    }
    ranked_stage1 = sorted(
        valid_lexical.items(), key=lambda item: (-item[1], item[0])
    )
    if len(ranked_stage1) >= 2:
        margin = ranked_stage1[0][1] - ranked_stage1[1][1]
    else:
        margin = 1.0
    ambiguous = len(ranked_stage1) >= 2 and margin <= policy.ambiguity_margin

    normalized_lexical = _normalize_scores(valid_lexical)
    clicked_scores = _click_scores(used, policy)
    candidates = dict(normalized_lexical)
    if candidates and ambiguous:
        for key in clicked_scores:
            if taxonomy.intent(*key) is not None:
                candidates.setdefault(key, 0.0)

    rerank_applied = bool(ambiguous and used and candidates)
    final_scores: dict[tuple[str, str], float] = {}
    if rerank_applied:
        for key in candidates:
            final_scores[key] = (
                (1.0 - policy.click_weight) * candidates[key]
                + policy.click_weight * clicked_scores.get(key, 0.0)
            )
    else:
        final_scores = dict(candidates)

    used_fallback = False
    if not final_scores:
        fallback = _fallback_intent(taxonomy)
        if fallback is not None:
            final_scores[fallback] = 0.0
            used_fallback = True

    ranked = sorted(final_scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked:
        raise RuntimeError("taxonomy has no usable fallback intent")
    maximum = ranked[0][1]
    exps = {
        key: math.exp((score - maximum) / policy.softmax_temperature)
        for key, score in ranked
    }
    total = sum(exps.values())
    probabilities = {key: value / total for key, value in exps.items()}
    if used_fallback:
        probabilities = {key: 0.0 for key in probabilities}

    top_k: list[dict[str, Any]] = []
    for key, score in ranked[: policy.top_k]:
        domain, intent_class = key
        top_k.append(
            {
                "domain": domain,
                "intent_class": intent_class,
                "score": round(score, 6),
                "confidence": round(probabilities[key], 6),
                "lexical_score": round(valid_lexical.get(key, 0.0), 6),
                "click_score": round(clicked_scores.get(key, 0.0), 6),
            }
        )
    top_key = (top_k[0]["domain"], top_k[0]["intent_class"])
    domain_scores: dict[str, float] = {}
    if not used_fallback:
        for key, probability in probabilities.items():
            domain_scores[key[0]] = max(
                domain_scores.get(key[0], 0.0), probability
            )
    domain_ranking = [
        {"domain": domain, "confidence": round(score, 6)}
        for domain, score in sorted(
            domain_scores.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    domain_confidence = domain_scores.get(top_key[0], 0.0)
    return {
        "domain": top_key[0],
        "intent_class": top_key[1],
        "intent_confidence": top_k[0]["confidence"],
        "domain_confidence": round(domain_confidence, 6),
        "domain_ranking": domain_ranking,
        "top_k_intents": top_k,
        "disambiguation": {
            "ambiguous": ambiguous,
            "stage1_margin": round(margin, 6),
            "ambiguity_margin": policy.ambiguity_margin,
            "rerank_applied": rerank_applied,
            "click_weight": policy.click_weight,
            "softmax_temperature": policy.softmax_temperature,
            "feedback_clicks_used": [
                {
                    "doc_id": click.get("doc_id"),
                    "rank": int(click.get("rank", 0)),
                    "dwell_ms": int(click.get("dwell_ms", 0)),
                    "domain": click.get("domain"),
                    "intent_class": click.get("intent_class"),
                }
                for click in used
            ],
            "feedback_clicks_excluded": excluded,
            "click_window": {
                "rank_max": policy.feedback_rank_max,
                "min_dwell_ms": policy.feedback_min_dwell_ms,
            },
            "confidence_policy": {
                "kind": "softmax",
                "temperature": policy.softmax_temperature,
                "score_mix": {
                    "lexical": round(1.0 - policy.click_weight, 6),
                    "pseudo_relevance": policy.click_weight,
                },
            },
            "used_fallback": used_fallback,
        },
        "stage1_candidates": [
            {
                "domain": key[0],
                "intent_class": key[1],
                "score": round(score, 6),
            }
            for key, score in ranked_stage1
        ],
    }


def _fallback_intent(taxonomy: Taxonomy) -> tuple[str, str] | None:
    for key in taxonomy.sorted_intents():
        if key == ("general", "unknown_query"):
            return key
    intents = taxonomy.sorted_intents()
    return intents[0] if intents else None

