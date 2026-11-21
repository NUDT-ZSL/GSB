"""Cross-path intent, slot, confidence and metric comparability adjudication."""

from __future__ import annotations

from typing import Any

import numpy as np

from .clicks import ClickPolicy
from .errors import E_PATH_INCOMPARABLE, error_dict
from .metrics import evaluate_results
from .pipeline import PATH_APPROX, PATH_EXACT, QueryEngine


def _metric_by_query(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {detail["query_id"]: detail for detail in report["details"]}


def _slot_map(slots: list[dict[str, Any]]) -> dict[tuple[str, int, int], dict[str, Any]]:
    return {
        (slot["slot_name"], int(slot["start"]), int(slot["end"])): slot
        for slot in slots
    }


def _slot_differences(
    exact_slots: list[dict[str, Any]],
    approx_slots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, bool]:
    exact_map = _slot_map(exact_slots)
    approx_map = _slot_map(approx_slots)
    differences: list[dict[str, Any]] = []
    offset_mismatch = False
    value_mismatch = False
    for key in sorted(set(exact_map) | set(approx_map)):
        exact = exact_map.get(key)
        approx = approx_map.get(key)
        if exact is None or approx is None:
            offset_mismatch = True
            differences.append(
                {
                    "kind": "exact_only" if exact else "approx_only",
                    "slot_name": key[0],
                    "exact": _slot_signature(exact),
                    "approx": _slot_signature(approx),
                }
            )
            continue
        if exact["normalized_value"] != approx["normalized_value"]:
            value_mismatch = True
            differences.append(
                {
                    "kind": "normalized_value",
                    "slot_name": key[0],
                    "start": key[1],
                    "end": key[2],
                    "exact_value": exact["normalized_value"],
                    "approx_value": approx["normalized_value"],
                }
            )
        if abs(float(exact["confidence"]) - float(approx["confidence"])) > 1e-6:
            differences.append(
                {
                    "kind": "slot_confidence",
                    "slot_name": key[0],
                    "start": key[1],
                    "end": key[2],
                    "exact_confidence": exact["confidence"],
                    "approx_confidence": approx["confidence"],
                }
            )
    return differences, offset_mismatch, value_mismatch


def _slot_signature(slot: dict[str, Any] | None) -> dict[str, Any] | None:
    if slot is None:
        return None
    return {
        "slot_name": slot["slot_name"],
        "start": slot["start"],
        "end": slot["end"],
        "normalized_value": slot["normalized_value"],
        "confidence": slot["confidence"],
    }


def compare_paths(
    engine: QueryEngine,
    queries: list[dict[str, Any]],
    *,
    click_policy: ClickPolicy | None = None,
    max_confidence_bias: float = 0.10,
    max_metric_delta: float = 0.05,
) -> dict[str, Any]:
    policy = click_policy or engine.click_policy
    exact_results = [
        engine.process(query, path=PATH_EXACT, click_policy=policy)
        for query in queries
    ]
    approx_results = [
        engine.process(query, path=PATH_APPROX, click_policy=policy)
        for query in queries
    ]
    exact_metrics = evaluate_results(exact_results, queries, policy)
    approx_metrics = evaluate_results(approx_results, queries, policy)
    exact_metric_by_query = _metric_by_query(exact_metrics)
    approx_metric_by_query = _metric_by_query(approx_metrics)

    per_query: list[dict[str, Any]] = []
    intent_matches = 0
    confidence_deltas: list[float] = []
    confidence_abs_deltas: list[float] = []
    first_incomparable: str | None = None
    offset_difference_count = 0
    value_difference_count = 0
    slot_confidence_difference_count = 0
    propagation_causes: dict[str, int] = {
        "candidate_pruning_miss": 0,
        "disambiguation_policy": 0,
        "slot_candidate_or_overlap_policy": 0,
        "confidence_scale": 0,
        "metric_delta": 0,
    }

    for exact, approx in zip(exact_results, approx_results):
        query_id = exact["query_id"]
        exact_intent = (exact["domain"], exact["intent_class"])
        approx_intent = (approx["domain"], approx["intent_class"])
        intent_equal = exact_intent == approx_intent
        if intent_equal:
            intent_matches += 1
        confidence_delta = round(
            float(approx["intent_confidence"]) - float(exact["intent_confidence"]), 6
        )
        confidence_deltas.append(confidence_delta)
        confidence_abs_deltas.append(abs(confidence_delta))
        slot_diffs, offset_mismatch, value_mismatch = _slot_differences(
            exact["slots"], approx["slots"]
        )
        offset_difference_count += sum(
            1 for row in slot_diffs if row["kind"] in {"exact_only", "approx_only"}
        )
        value_difference_count += sum(
            1 for row in slot_diffs if row["kind"] == "normalized_value"
        )
        slot_confidence_difference_count += sum(
            1 for row in slot_diffs if row["kind"] == "slot_confidence"
        )
        max_slot_confidence_delta = max(
            (
                abs(
                    float(row["approx_confidence"])
                    - float(row["exact_confidence"])
                )
                for row in slot_diffs
                if row["kind"] == "slot_confidence"
            ),
            default=0.0,
        )
        causes: list[str] = []
        approx_candidates = {
            (row["domain"], row["intent_class"])
            for row in approx["debug"]["candidate_intents"]
        }
        if not intent_equal:
            if exact_intent not in approx_candidates:
                causes.append("candidate_pruning_miss")
            else:
                causes.append("disambiguation_policy")
        elif slot_diffs:
            causes.append("slot_candidate_or_overlap_policy")
        if abs(confidence_delta) > 1e-6:
            causes.append("confidence_scale")
        exact_metric = exact_metric_by_query[query_id]
        approx_metric = approx_metric_by_query[query_id]
        query_metric_delta = max(
            abs(float(exact_metric["ndcg_at_k"]) - float(approx_metric["ndcg_at_k"])),
            abs(float(exact_metric["mrr"]) - float(approx_metric["mrr"])),
        )
        if query_metric_delta > max_metric_delta:
            causes.append("metric_delta")
        for cause in set(causes):
            propagation_causes[cause] += 1
        query_comparable = (
            intent_equal
            and not offset_mismatch
            and not value_mismatch
            and abs(confidence_delta) <= max_confidence_bias
            and max_slot_confidence_delta <= max_confidence_bias
            and query_metric_delta <= max_metric_delta
        )
        if not query_comparable and first_incomparable is None:
            first_incomparable = query_id
        per_query.append(
            {
                "query_id": query_id,
                "exact_intent": {
                    "domain": exact_intent[0],
                    "intent_class": exact_intent[1],
                },
                "approx_intent": {
                    "domain": approx_intent[0],
                    "intent_class": approx_intent[1],
                },
                "intent_equal": intent_equal,
                "slot_differences": slot_diffs,
                "offset_mismatch": offset_mismatch,
                "value_mismatch": value_mismatch,
                "confidence_delta_approx_minus_exact": confidence_delta,
                "metric_delta": round(query_metric_delta, 6),
                "causes": causes,
                "comparable": query_comparable,
            }
        )

    ndcg_delta = round(
        float(approx_metrics["aggregate"]["ndcg_at_k"])
        - float(exact_metrics["aggregate"]["ndcg_at_k"]),
        6,
    )
    mrr_delta = round(
        float(approx_metrics["aggregate"]["mrr"])
        - float(exact_metrics["aggregate"]["mrr"]),
        6,
    )
    aggregate_metric_delta = max(abs(ndcg_delta), abs(mrr_delta))
    delta_array = np.asarray(confidence_deltas, dtype=float)
    if delta_array.size:
        bias_stats = {
            "count": int(delta_array.size),
            "mean": round(float(np.mean(delta_array)), 6),
            "mean_abs": round(float(np.mean(np.abs(delta_array))), 6),
            "p50": round(float(np.percentile(delta_array, 50)), 6),
            "p90_abs": round(float(np.percentile(np.abs(delta_array), 90)), 6),
            "max_abs": round(float(np.max(np.abs(delta_array))), 6),
        }
        counts, edges = np.histogram(
            delta_array,
            bins=[-1.0, -0.25, -0.10, -0.02, 0.02, 0.10, 0.25, 1.0],
        )
        spectrum = [
            {
                "lower": round(float(edges[index]), 6),
                "upper": round(float(edges[index + 1]), 6),
                "count": int(counts[index]),
            }
            for index in range(len(counts))
        ]
    else:
        bias_stats = {
            "count": 0,
            "mean": 0.0,
            "mean_abs": 0.0,
            "p50": 0.0,
            "p90_abs": 0.0,
            "max_abs": 0.0,
        }
        spectrum = []

    comparable = (
        intent_matches == len(queries)
        and offset_difference_count == 0
        and value_difference_count == 0
        and slot_confidence_difference_count == 0
        and bias_stats["max_abs"] <= max_confidence_bias
        and aggregate_metric_delta <= max_metric_delta
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "query_count": len(queries),
        "paths": [PATH_EXACT, PATH_APPROX],
        "policy": {
            "require_intent_parity": True,
            "require_slot_offset_parity": True,
            "require_slot_value_parity": True,
            "max_confidence_bias": max_confidence_bias,
            "max_metric_delta": max_metric_delta,
        },
        "intent_agreement_rate": round(intent_matches / len(queries), 6)
        if queries
        else 1.0,
        "intent_mismatch_count": len(queries) - intent_matches,
        "slot_offset_difference_count": offset_difference_count,
        "slot_value_difference_count": value_difference_count,
        "slot_confidence_difference_count": slot_confidence_difference_count,
        "confidence_bias_spectrum": {
            "statistics": bias_stats,
            "histogram": spectrum,
        },
        "metrics": {
            PATH_EXACT: exact_metrics,
            PATH_APPROX: approx_metrics,
            "delta_approx_minus_exact": {
                "ndcg_at_k": ndcg_delta,
                "mrr": mrr_delta,
            },
        },
        "difference_causes": propagation_causes,
        "per_query": per_query,
        "comparable": comparable,
        "first_incomparable_query": None if comparable else first_incomparable,
        "ok": comparable,
        "error_code": None,
    }
    if not comparable:
        report.update(
            error_dict(
                E_PATH_INCOMPARABLE,
                "exact and approximate paths no longer share comparable intent/slot/metric semantics",
                stage="compare",
                category="path_comparability",
                details={
                    "first_incomparable_query": first_incomparable,
                    "intent_mismatch_count": len(queries) - intent_matches,
                    "slot_offset_difference_count": offset_difference_count,
                    "slot_value_difference_count": value_difference_count,
                    "slot_confidence_difference_count": slot_confidence_difference_count,
                    "max_confidence_bias": bias_stats["max_abs"],
                    "aggregate_metric_delta": aggregate_metric_delta,
                },
            )
        )
        report["comparable"] = False
        report["ok"] = False
        report["error_code"] = E_PATH_INCOMPARABLE
    return report

