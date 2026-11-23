"""Stable threshold comparison and deterministic top-k aggregation."""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from .canonical import (
    DEFAULT_DECIMALS,
    DEFAULT_SUBNORMAL_FLOOR,
    stable_float,
)
from .errors import E_CONFIG, RecallError
from .projection import project_query, sparse_dot
from .weights import WeightTable


STRICT = "strict"
TOLERANCE = "tolerance"
COMPARE_MODES = {STRICT, TOLERANCE}


def sanitize_score(
    score: float,
    *,
    decimals: int = DEFAULT_DECIMALS,
    subnormal_floor: float = DEFAULT_SUBNORMAL_FLOOR,
) -> float:
    if not math.isfinite(score):
        raise RecallError(E_CONFIG, "scoring", "similarity score is not finite")
    return stable_float(score, decimals=decimals, subnormal_floor=subnormal_floor)


def threshold_passes(
    score: float,
    threshold: float | None,
    *,
    mode: str = STRICT,
    tolerance: float = 0.0,
) -> bool:
    if threshold is None:
        return True
    if mode not in COMPARE_MODES:
        raise RecallError(E_CONFIG, "scoring", f"unknown threshold mode {mode!r}")
    if tolerance < 0 or not math.isfinite(tolerance):
        raise RecallError(E_CONFIG, "scoring", "tolerance must be finite and >= 0")
    if mode == STRICT:
        return score > threshold
    return score >= threshold - tolerance


def _ordering_bucket(score: float, tolerance: float) -> int:
    if tolerance <= 0:
        return 0
    return int(math.floor((score + tolerance * 0.5) / tolerance))


def rank_scores(
    scores: Iterable[tuple[str, float]],
    *,
    top_k: int,
    threshold: float | None = None,
    mode: str = STRICT,
    tolerance: float = 0.0,
    decimals: int = DEFAULT_DECIMALS,
    subnormal_floor: float = DEFAULT_SUBNORMAL_FLOOR,
) -> list[dict[str, Any]]:
    if top_k < 1:
        raise RecallError(E_CONFIG, "scoring", "top_k must be >= 1")
    candidates: list[tuple[str, float]] = []
    for doc_id, raw_score in scores:
        score = sanitize_score(
            raw_score,
            decimals=decimals,
            subnormal_floor=subnormal_floor,
        )
        if threshold_passes(score, threshold, mode=mode, tolerance=tolerance):
            candidates.append((str(doc_id), score))

    if mode == STRICT:
        ordered = sorted(candidates, key=lambda item: (-item[1], item[0]))
    else:
        ordered = sorted(
            candidates,
            key=lambda item: (
                -_ordering_bucket(item[1], tolerance),
                item[0],
            ),
        )
    selected = ordered[:top_k]
    return [
        {
            "rank": rank,
            "doc_id": doc_id,
            "score": score,
        }
        for rank, (doc_id, score) in enumerate(selected, start=1)
    ]


def score_query(
    query: Any,
    doc_vectors: Sequence[dict[str, Any]],
    weights: WeightTable,
    *,
    executed_top_k: int,
    threshold: float | None,
    compare_mode: str,
    tolerance: float,
    decimals: int = DEFAULT_DECIMALS,
    subnormal_floor: float = DEFAULT_SUBNORMAL_FLOOR,
) -> tuple[dict[str, Any], dict[str, Any]]:
    query_vector = project_query(
        query,
        weights,
        decimals=decimals,
        subnormal_floor=subnormal_floor,
    )
    scores = [
        (
            str(vector["doc_id"]),
            sparse_dot(
                query_vector["dims"],
                vector["dims"],
                decimals=decimals,
                subnormal_floor=subnormal_floor,
            ),
        )
        for vector in doc_vectors
    ]
    ranked = rank_scores(
        scores,
        top_k=executed_top_k,
        threshold=threshold,
        mode=compare_mode,
        tolerance=tolerance,
        decimals=decimals,
        subnormal_floor=subnormal_floor,
    )
    result = {
        "query_id": query.query_id,
        "batch_no": query.batch_no,
        "priority": query.priority,
        "requested_top_k": query.top_k,
        "executed_top_k": executed_top_k,
        "threshold": threshold,
        "compare_mode": compare_mode,
        "tolerance": stable_float(tolerance, decimals),
        "candidate_count": len(scores),
        "returned_count": len(ranked),
        "top_k": ranked,
    }
    return result, query_vector
