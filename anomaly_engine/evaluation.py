"""Quality metrics, set consistency checks and execution deviation spectra."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable

import numpy as np

from .canonical import SCORE_DECIMALS, SCORE_TOLERANCE, canonical_json_bytes
from .errors import EngineError


def _rounded(value: float) -> float:
    rounded = round(float(value), SCORE_DECIMALS)
    return 0.0 if rounded == 0.0 else rounded


def _metric_order(rows: list[tuple[str, float]]) -> list[tuple[str, float]]:
    return sorted(rows, key=lambda item: (-item[1], item[0]))


def compute_metrics(
    score_details: Iterable[dict[str, Any]],
    injected_by_id: dict[str, bool],
    *,
    quota_fraction: float,
) -> dict[str, Any]:
    rows: list[tuple[str, float]] = []
    for detail in score_details:
        sample_id = detail["sample_id"]
        score = float(detail["score"])
        if not math.isfinite(score):
            raise EngineError(
                "E_INCONSISTENT",
                "evaluation",
                "cannot evaluate a non-finite score",
                details={"category": "non_finite_score", "sample_id": sample_id},
            )
        rows.append((sample_id, score))
    rows = _metric_order(rows)
    positives = [score for sample_id, score in rows if injected_by_id[sample_id]]
    negatives = [score for sample_id, score in rows if not injected_by_id[sample_id]]
    n_pos = len(positives)
    n_neg = len(negatives)

    if n_pos == 0 or n_neg == 0:
        auc: float | None = None
    else:
        wins = 0.0
        for positive in positives:
            for negative in negatives:
                if positive > negative:
                    wins += 1.0
                elif positive == negative:
                    wins += 0.5
        auc = wins / float(n_pos * n_neg)

    if n_pos == 0:
        average_precision: float | None = None
    else:
        true_positive = 0
        false_positive = 0
        previous_recall = 0.0
        average_precision = 0.0
        index = 0
        while index < len(rows):
            score = rows[index][1]
            group_end = index
            while group_end < len(rows) and rows[group_end][1] == score:
                sample_id = rows[group_end][0]
                if injected_by_id[sample_id]:
                    true_positive += 1
                else:
                    false_positive += 1
                group_end += 1
            recall = true_positive / n_pos
            precision = true_positive / float(true_positive + false_positive)
            average_precision += (recall - previous_recall) * precision
            previous_recall = recall
            index = group_end

    n_rows = len(rows)
    quota = max(1, min(n_rows, int(math.ceil(quota_fraction * n_rows))))
    selected = rows[:quota]
    detected = sum(1 for sample_id, _ in selected if injected_by_id[sample_id])
    false_positives = quota - detected

    return {
        "score_semantics": "higher_is_more_anomalous",
        "n_samples": n_rows,
        "n_injected_anomalies": n_pos,
        "auc": None if auc is None else _rounded(auc),
        "average_precision": (
            None if average_precision is None else _rounded(average_precision)
        ),
        "quota_fraction": round(float(quota_fraction), SCORE_DECIMALS),
        "quota": quota,
        "detected_at_quota": detected,
        "false_positives_at_quota": false_positives,
        "detection_rate_at_quota": _rounded(detected / n_pos) if n_pos else None,
        "precision_at_quota": _rounded(detected / quota),
        "recall_at_quota": _rounded(detected / n_pos) if n_pos else None,
    }


def assert_result_consistency(
    result: dict[str, Any],
    *,
    expected_ids: tuple[str, ...],
    injected_by_id: dict[str, bool],
    quota_fraction: float,
) -> dict[str, Any]:
    expected = set(expected_ids)
    categories: list[str] = []
    evidence: dict[str, Any] = {}
    details = result.get("score_details", [])
    ranking = result.get("ranking", [])
    score_ids = [detail.get("sample_id") for detail in details]
    score_id_counts = Counter(score_ids)
    rank_counts = Counter(ranking)

    if len(score_ids) != len(expected) or set(score_ids) != expected:
        categories.append("sample_set_mismatch")
        evidence["missing_score_ids"] = sorted(expected - set(score_ids))[:20]
        evidence["unexpected_score_ids"] = sorted(set(score_ids) - expected)[:20]
    if any(count != 1 for count in score_id_counts.values()):
        categories.append("duplicate_score_detail")
        evidence["duplicate_score_ids"] = sorted(
            sample_id for sample_id, count in score_id_counts.items() if count != 1
        )[:20]
    if len(ranking) != len(expected) or set(ranking) != expected:
        categories.append("ranking_set_mismatch")
        evidence["missing_ranked_ids"] = sorted(expected - set(ranking))[:20]
        evidence["unexpected_ranked_ids"] = sorted(set(ranking) - expected)[:20]
    if any(count != 1 for count in rank_counts.values()):
        categories.append("duplicate_ranking_entry")
        evidence["duplicate_ranked_ids"] = sorted(
            sample_id for sample_id, count in rank_counts.items() if count != 1
        )[:20]

    for detail in details:
        score = detail.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            categories.append("non_finite_score")
            evidence["first_non_finite_sample_id"] = detail.get("sample_id")
            break

    if not categories:
        expected_ranking = [
            sample_id
            for sample_id, _ in sorted(
                ((detail["sample_id"], float(detail["score"])) for detail in details),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        if list(ranking) != expected_ranking:
            categories.append("ranking_order_mismatch")
            first = next(
                index
                for index, (actual, wanted) in enumerate(zip(ranking, expected_ranking))
                if actual != wanted
            )
            evidence["first_rank_mismatch_index"] = first
            evidence["actual_sample_id"] = ranking[first]
            evidence["expected_sample_id"] = expected_ranking[first]

    if categories:
        raise EngineError(
            "E_INCONSISTENT",
            "consistency_check",
            "detection result violates the result contract",
            method=result.get("method"),
            details={
                "categories": sorted(set(categories)),
                "candidate_id": result.get("candidate_id"),
                **evidence,
            },
        )

    recomputed = compute_metrics(
        details,
        injected_by_id,
        quota_fraction=quota_fraction,
    )
    if canonical_json_bytes(recomputed) != canonical_json_bytes(result.get("metrics")):
        raise EngineError(
            "E_INCONSISTENT",
            "metric_recheck",
            "metrics cannot be independently reproduced from score details",
            method=result.get("method"),
            details={
                "category": "metric_recompute_mismatch",
                "candidate_id": result.get("candidate_id"),
                "declared": result.get("metrics"),
                "recomputed": recomputed,
            },
        )
    return {
        "ok": True,
        "sample_set_equal": True,
        "scores_finite": True,
        "ranking_complete_and_unique": True,
        "metrics_recomputed_equal": True,
    }


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    return float(np.quantile(np.asarray(values, dtype=np.float64), probability, method="linear"))


def compare_results(
    candidate_result: dict[str, Any],
    baseline_result: dict[str, Any],
    *,
    tolerance: float = SCORE_TOLERANCE,
) -> dict[str, Any]:
    candidate_scores = {
        detail["sample_id"]: float(detail["score"])
        for detail in candidate_result.get("score_details", [])
    }
    baseline_scores = {
        detail["sample_id"]: float(detail["score"])
        for detail in baseline_result.get("score_details", [])
    }
    common_ids = sorted(set(candidate_scores) & set(baseline_scores))
    deltas = [abs(candidate_scores[sample_id] - baseline_scores[sample_id]) for sample_id in common_ids]
    first_score_mismatch = next(
        (
            {
                "sample_id": sample_id,
                "candidate_score": candidate_scores[sample_id],
                "baseline_score": baseline_scores[sample_id],
                "abs_delta": abs(candidate_scores[sample_id] - baseline_scores[sample_id]),
            }
            for sample_id in common_ids
            if abs(candidate_scores[sample_id] - baseline_scores[sample_id]) > tolerance
        ),
        None,
    )

    candidate_ranking = list(candidate_result.get("ranking", []))
    baseline_ranking = list(baseline_result.get("ranking", []))
    rank_positions_candidate = {
        sample_id: index for index, sample_id in enumerate(candidate_ranking)
    }
    rank_positions_baseline = {
        sample_id: index for index, sample_id in enumerate(baseline_ranking)
    }
    first_rank_mismatch: dict[str, Any] | None = None
    for index in range(max(len(candidate_ranking), len(baseline_ranking))):
        candidate_id = candidate_ranking[index] if index < len(candidate_ranking) else None
        baseline_id = baseline_ranking[index] if index < len(baseline_ranking) else None
        if candidate_id != baseline_id:
            first_rank_mismatch = {
                "index": index,
                "candidate_sample_id": candidate_id,
                "baseline_sample_id": baseline_id,
            }
            break

    thresholds = [0.0, 1e-15, 1e-12, 1e-9, 1e-6, math.inf]
    histogram: list[dict[str, Any]] = []
    for lower, upper in zip(thresholds[:-1], thresholds[1:]):
        if lower == 0.0:
            count = sum(1 for delta in deltas if delta == 0.0)
            label = "=0"
        elif math.isinf(upper):
            count = sum(1 for delta in deltas if lower < delta)
            label = f">{lower}"
        else:
            count = sum(1 for delta in deltas if lower < delta <= upper)
            label = f"({lower},{upper}]"
        histogram.append({"bucket": label, "count": count})

    score_mismatch_count = sum(1 for delta in deltas if delta > tolerance)
    rankings_equal = candidate_ranking == baseline_ranking
    nonzero_sample_deltas = [
        {
            "sample_id": sample_id,
            "candidate_score": candidate_scores[sample_id],
            "baseline_score": baseline_scores[sample_id],
            "abs_delta": abs(candidate_scores[sample_id] - baseline_scores[sample_id]),
        }
        for sample_id in common_ids
        if abs(candidate_scores[sample_id] - baseline_scores[sample_id]) > 0.0
    ]
    first_inconsistency = first_rank_mismatch or first_score_mismatch
    if first_rank_mismatch is not None:
        first_inconsistency = {"kind": "rank", **first_rank_mismatch}
    elif first_score_mismatch is not None:
        first_inconsistency = {"kind": "score", **first_score_mismatch}
    consistent = (
        set(candidate_scores) == set(baseline_scores)
        and score_mismatch_count == 0
        and rankings_equal
    )
    return {
        "consistent": consistent,
        "declared_tolerance": tolerance,
        "score_checksum_equal": candidate_result.get("score_checksum")
        == baseline_result.get("score_checksum"),
        "ranking_equal": rankings_equal,
        "n_compared_samples": len(common_ids),
        "score_mismatch_count": score_mismatch_count,
        "max_abs_delta": max(deltas, default=0.0),
        "mean_abs_delta": (sum(deltas) / len(deltas)) if deltas else 0.0,
        "p50_abs_delta": _quantile(deltas, 0.50),
        "p95_abs_delta": _quantile(deltas, 0.95),
        "first_score_mismatch": first_score_mismatch,
        "first_rank_mismatch": first_rank_mismatch,
        "first_inconsistency": first_inconsistency,
        "nonzero_delta_count": len(nonzero_sample_deltas),
        "nonzero_sample_deltas": nonzero_sample_deltas,
        "abs_delta_histogram": histogram,
        "candidate_rank_positions": rank_positions_candidate,
        "baseline_rank_positions": rank_positions_baseline,
    }
