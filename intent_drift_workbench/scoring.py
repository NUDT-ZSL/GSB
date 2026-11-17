"""Stable window distributions, drift scoring, and contribution allocation."""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

from .canonical import quantize
from .features import VocabState


def _distribution(category_counts: Iterable[int]) -> tuple[np.ndarray, bool]:
    counts = np.asarray(list(category_counts), dtype=np.float64)
    total = float(counts.sum())
    if total <= 0.0:
        return np.full(len(counts), 1.0 / len(counts), dtype=np.float64), True
    return counts / total, False


def _quantized_distribution(distribution: np.ndarray, precision: int) -> list[float]:
    values = [quantize(float(item), precision) for item in distribution]
    return [0.0 if item == 0 else item for item in values]


def _js_category_contributions(current: np.ndarray, previous: np.ndarray) -> np.ndarray:
    midpoint = (current + previous) / 2.0
    output = np.zeros_like(current)
    for index in range(len(current)):
        if current[index] > 0.0:
            output[index] += 0.5 * current[index] * math.log2(current[index] / midpoint[index])
        if previous[index] > 0.0:
            output[index] += 0.5 * previous[index] * math.log2(previous[index] / midpoint[index])
    return output


def _category_term_allocation(
    term_counts_current: dict[str, int],
    term_counts_previous: dict[str, int],
    category: str,
    vocab: VocabState,
    total_current: float,
    total_previous: float,
) -> dict[str, float]:
    candidates = sorted(
        term
        for term in set(term_counts_current) | set(term_counts_previous)
        if vocab.category_by_term.get(term) == category
    )
    if not candidates:
        return {f"__category__:{category}": 1.0}
    weights: dict[str, float] = {}
    for term in candidates:
        current_share = (
            float(term_counts_current.get(term, 0)) / total_current
            if total_current > 0.0
            else 0.0
        )
        previous_share = (
            float(term_counts_previous.get(term, 0)) / total_previous
            if total_previous > 0.0
            else 0.0
        )
        weights[term] = max(current_share, previous_share)
    denominator = math.fsum(weights.values())
    if denominator <= 0.0:
        return {f"__category__:{category}": 1.0}
    return {term: weight / denominator for term, weight in weights.items()}


def score_window(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    vocab: VocabState,
    weights: dict[str, float],
    threshold: float,
    precision: int,
    baseline_status: str,
) -> dict[str, Any]:
    current_distribution, current_empty = _distribution(current["category_counts"])
    if previous is None:
        previous_distribution = np.zeros_like(current_distribution)
        previous_empty = True
        l1 = 0.0
        js = 0.0
        novelty = 0.0
        js_categories = np.zeros_like(current_distribution)
        score = 0.0
        contributions: dict[str, dict[str, float]] = {}
    else:
        previous_distribution, previous_empty = _distribution(previous["category_counts"])
        difference = np.abs(current_distribution - previous_distribution)
        l1 = 0.5 * float(np.sum(difference))
        js_categories = _js_category_contributions(current_distribution, previous_distribution)
        js = float(np.sum(js_categories))
        current_total = float(sum(current["term_counts"].values()))
        previous_terms = set(previous["term_counts"])
        new_terms = [term for term in current["term_counts"] if term not in previous_terms]
        new_count = float(sum(current["term_counts"][term] for term in new_terms))
        novelty = new_count / current_total if current_total > 0.0 else 0.0

        total_current = float(sum(current["term_counts"].values()))
        total_previous = float(sum(previous["term_counts"].values()))
        contributions = {}
        all_terms = sorted(set(current["term_counts"]) | set(previous["term_counts"]))
        for term in all_terms:
            contributions[term] = {
                "distribution_l1": 0.0,
                "js_divergence": 0.0,
                "novelty": 0.0,
                "total": 0.0,
            }
        for category_index, category in enumerate(vocab.categories):
            allocation = _category_term_allocation(
                current["term_counts"],
                previous["term_counts"],
                category,
                vocab,
                total_current,
                total_previous,
            )
            l1_category = weights["distribution_l1"] * 0.5 * float(difference[category_index])
            js_category = weights["js_divergence"] * float(js_categories[category_index])
            for term, share in allocation.items():
                entry = contributions.setdefault(
                    term,
                    {
                        "distribution_l1": 0.0,
                        "js_divergence": 0.0,
                        "novelty": 0.0,
                        "total": 0.0,
                    },
                )
                entry["distribution_l1"] += l1_category * share
                entry["js_divergence"] += js_category * share
        if total_current > 0.0:
            for term in new_terms:
                contributions.setdefault(
                    term,
                    {
                        "distribution_l1": 0.0,
                        "js_divergence": 0.0,
                        "novelty": 0.0,
                        "total": 0.0,
                    },
                )["novelty"] += (
                    weights["novelty"] * float(current["term_counts"][term]) / total_current
                )
        score = (
            weights["distribution_l1"] * l1
            + weights["js_divergence"] * js
            + weights["novelty"] * novelty
        )

    quantized_contributions: dict[str, dict[str, float]] = {}
    for term in sorted(contributions):
        entry = contributions[term]
        total = math.fsum(entry[key] for key in ("distribution_l1", "js_divergence", "novelty"))
        quantized_contributions[term] = {
            "distribution_l1": quantize(entry["distribution_l1"], precision),
            "js_divergence": quantize(entry["js_divergence"], precision),
            "novelty": quantize(entry["novelty"], precision),
            "total": quantize(total, precision),
        }
    contribution_sum = quantize(
        math.fsum(item["total"] for item in quantized_contributions.values()), precision
    )
    rounded_score = quantize(score, precision)
    rounded_threshold = quantize(threshold, precision)
    return {
        "baseline_status": baseline_status,
        "distribution": _quantized_distribution(current_distribution, precision),
        "distribution_empty": current_empty,
        "previous_distribution": (
            _quantized_distribution(previous_distribution, precision)
            if previous is not None
            else None
        ),
        "previous_distribution_empty": previous_empty if previous is not None else None,
        "components": {
            "distribution_l1": quantize(weights["distribution_l1"] * l1, precision),
            "js_divergence": quantize(weights["js_divergence"] * js, precision),
            "novelty": quantize(weights["novelty"] * novelty, precision),
        },
        "raw_components": {
            "distribution_l1": l1,
            "js_divergence": js,
            "novelty": novelty,
        },
        "drift_score": rounded_score,
        "alert": bool(previous is not None and rounded_score >= rounded_threshold),
        "boundary_equal_threshold": bool(
            previous is not None and rounded_score == rounded_threshold
        ),
        "threshold": rounded_threshold,
        "contributions": quantized_contributions,
        "contribution_sum": contribution_sum,
        "rounding_residual": quantize(rounded_score - contribution_sum, precision),
    }


def alert_sort_key(window: dict[str, Any], *, tie_by_start: bool = True) -> tuple[Any, ...]:
    if tie_by_start:
        return (
            -float(window["drift_score"]),
            int(window["start_turn"]),
            str(window["session_id"]),
        )
    return (
        -float(window["drift_score"]),
        str(window["session_id"]),
        int(window["start_turn"]),
    )


def label_alerts(windows: list[dict[str, Any]], threshold: float, precision: int) -> list[dict[str, Any]]:
    full_windows = [item for item in windows if item.get("status") == "full"]
    alerts = [item for item in full_windows if item.get("alert")]
    ranked = sorted(alerts, key=alert_sort_key)
    for rank, window in enumerate(ranked, start=1):
        window["alert_rank"] = rank
    for window in full_windows:
        window.setdefault("alert_rank", None)
    boundary = [
        {
            "window_id": item["window_id"],
            "session_id": item["session_id"],
            "start_turn": item["start_turn"],
            "drift_score": item["drift_score"],
            "threshold": quantize(threshold, precision),
        }
        for item in sorted(
            (candidate for candidate in full_windows if candidate.get("boundary_equal_threshold")),
            key=lambda candidate: (int(candidate["start_turn"]), str(candidate["session_id"])),
        )
    ]
    return boundary
