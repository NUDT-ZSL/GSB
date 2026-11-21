"""Offline click-based intent metrics with independently recomputable details."""

from __future__ import annotations

import math
from typing import Any, Iterable

from .clicks import ClickPolicy, eval_clicks
from .errors import E_METRIC_MISMATCH, QUError


def _dcg(grades: Iterable[int]) -> float:
    total = 0.0
    for index, grade in enumerate(grades):
        gain = (2**grade) - 1
        total += gain / math.log2(index + 2)
    return total


def _grade_map(clicks: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for click in clicks:
        key = (str(click.get("domain", "")), str(click.get("intent_class", "")))
        counts[key] = counts.get(key, 0) + 1
    return {
        key: min(3, value)
        for key, value in sorted(counts.items())
    }


def _metric_from_detail(detail: dict[str, Any], k: int) -> tuple[float, float, float, int | None]:
    relevant = {
        (row["domain"], row["intent_class"]): int(row["grade"])
        for row in detail["relevance_by_intent"]
    }
    top = detail["top_k_intents"][:k]
    top_grades = [relevant.get((row["domain"], row["intent_class"]), 0) for row in top]
    dcg = _dcg(top_grades)
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = _dcg(ideal)
    ndcg = dcg / idcg if idcg > 0 else 0.0
    first_relevant = None
    for index, grade in enumerate(top_grades):
        if grade > 0:
            first_relevant = index + 1
            break
    return dcg, idcg, ndcg, first_relevant


def evaluate_results(
    results: list[dict[str, Any]],
    queries: list[dict[str, Any]],
    policy: ClickPolicy,
) -> dict[str, Any]:
    query_by_id = {str(query["query_id"]): query for query in queries}
    details: list[dict[str, Any]] = []
    included_ndcg: list[float] = []
    included_mrr: list[float] = []
    for result in results:
        query_id = str(result["query_id"])
        query = query_by_id[query_id]
        clicks = eval_clicks(query.get("raw_clicks", []), policy)
        relevance = _grade_map(clicks)
        top_k = [
            {
                "domain": row["domain"],
                "intent_class": row["intent_class"],
                "confidence": row["confidence"],
            }
            for row in result["top_k_intents"][: policy.top_k]
        ]
        detail: dict[str, Any] = {
            "query_id": query_id,
            "top_k_intents": top_k,
            "relevance_by_intent": [
                {
                    "domain": domain,
                    "intent_class": intent,
                    "grade": grade,
                    "click_count": grade,
                }
                for (domain, intent), grade in relevance.items()
            ],
            "eval_click_count": len(clicks),
            "eval_click_doc_ids": [click.get("doc_id") for click in clicks],
            "included": bool(relevance),
            "deduplicated_click_grade_cap": 3,
            "ndcg_at_k": 0.0,
            "mrr": 0.0,
            "dcg": 0.0,
            "idcg": 0.0,
            "first_relevant_rank": None,
        }
        if relevance:
            dcg, idcg, ndcg, first_relevant = _metric_from_detail(
                detail, policy.top_k
            )
            detail["dcg"] = round(dcg, 6)
            detail["idcg"] = round(idcg, 6)
            detail["ndcg_at_k"] = round(ndcg, 6)
            detail["mrr"] = round(1.0 / first_relevant if first_relevant else 0.0, 6)
            detail["first_relevant_rank"] = first_relevant
            included_ndcg.append(round(ndcg, 6))
            included_mrr.append(detail["mrr"])
        details.append(detail)

    aggregate = {
        "query_count": len(results),
        "evaluated_query_count": len(included_ndcg),
        "excluded_query_count": len(results) - len(included_ndcg),
        "ndcg_at_k": round(sum(included_ndcg) / len(included_ndcg), 6)
        if included_ndcg
        else 0.0,
        "mrr": round(sum(included_mrr) / len(included_mrr), 6)
        if included_mrr
        else 0.0,
    }
    return {
        "k": policy.top_k,
        "click_window": {
            "feedback_rank_max": policy.feedback_rank_max,
            "feedback_min_dwell_ms": policy.feedback_min_dwell_ms,
            "eval_rank_min": policy.eval_rank_min,
            "eval_rank_max": policy.eval_rank_max,
        },
        "confidence_policy": {
            "kind": "softmax",
            "temperature": policy.softmax_temperature,
            "click_weight": policy.click_weight,
            "ambiguity_margin": policy.ambiguity_margin,
        },
        "relevance_policy": {
            "source": "eval_rank_window_clicks",
            "grade": "min(3, click_count)",
            "gain": "2^grade - 1",
            "discount": "log2(rank + 1)",
            "aggregation": "macro_mean_over_queries_with_eval_clicks",
        },
        "aggregate": aggregate,
        "details": details,
    }


def recompute_metrics(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rebuilt: dict[str, dict[str, Any]] = {}
    for detail in report["details"]:
        if not detail["included"]:
            rebuilt[detail["query_id"]] = {
                "ndcg_at_k": 0.0,
                "mrr": 0.0,
                "dcg": 0.0,
                "idcg": 0.0,
                "first_relevant_rank": None,
            }
            continue
        dcg, idcg, ndcg, first_relevant = _metric_from_detail(detail, int(report["k"]))
        rebuilt[detail["query_id"]] = {
            "ndcg_at_k": round(ndcg, 6),
            "mrr": round(1.0 / first_relevant if first_relevant else 0.0, 6),
            "dcg": round(dcg, 6),
            "idcg": round(idcg, 6),
            "first_relevant_rank": first_relevant,
        }
    for detail in report["details"]:
        expected = rebuilt[detail["query_id"]]
        for field, value in expected.items():
            if detail[field] != value:
                raise QUError(
                    E_METRIC_MISMATCH,
                    "per-query metric cannot be independently recomputed",
                    stage="metrics",
                    details={
                        "query_id": detail["query_id"],
                        "field": field,
                        "stored": detail[field],
                        "recomputed": value,
                    },
                )
    included = [detail for detail in report["details"] if detail["included"]]
    aggregate = {
        "ndcg_at_k": round(
            sum(detail["ndcg_at_k"] for detail in included) / len(included), 6
        )
        if included
        else 0.0,
        "mrr": round(sum(detail["mrr"] for detail in included) / len(included), 6)
        if included
        else 0.0,
    }
    return {
        "per_query": rebuilt,
        "aggregate": aggregate,
        "consistent": aggregate["ndcg_at_k"] == report["aggregate"]["ndcg_at_k"]
        and aggregate["mrr"] == report["aggregate"]["mrr"],
    }

