"""Numerically stable clone-similarity thresholding and top-k ranking."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .config import SimilarityConfig


@dataclass(frozen=True)
class CloneCandidateScore:
    candidate_id: str
    similarity: float
    metadata: dict[str, Any]


def sanitize_similarity(value: float, config: SimilarityConfig) -> tuple[float, bool, bool]:
    """Clamp cosine range and collapse subnormal magnitudes to exactly zero."""

    number = float(value)
    nonfinite = not math.isfinite(number)
    if nonfinite:
        return -1.0, False, True
    tiny = float(np.finfo(np.float64).tiny)
    floor = max(float(config.subnormal_floor), tiny)
    guarded = False
    if 0.0 < abs(number) < floor:
        number = 0.0
        guarded = True
    if number < -1.0:
        number = -1.0
    elif number > 1.0:
        number = 1.0
    return float(number), guarded, nonfinite


def _accept(similarity: float, config: SimilarityConfig) -> tuple[bool, bool, str]:
    distance = abs(similarity - config.threshold)
    near_threshold = distance <= config.tolerance
    if config.compare_strategy == "strict":
        return similarity > config.threshold, near_threshold, "strict_greater_than"
    accepted = similarity >= config.threshold - config.tolerance
    return accepted, near_threshold, "tolerance_band_inclusive"


def rank_clone_scores(
    candidates: Iterable[CloneCandidateScore],
    config: SimilarityConfig,
    *,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config.validate()
    prepared: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        candidate_id = str(candidate.candidate_id)
        if candidate_id in seen_ids:
            raise ValueError(f"duplicate clone candidate_id: {candidate_id}")
        seen_ids.add(candidate_id)
        stable, guarded, nonfinite = sanitize_similarity(candidate.similarity, config)
        accepted, near_threshold, rule = _accept(stable, config)
        prepared.append(
            {
                "candidate_id": candidate_id,
                "similarity_input": float(candidate.similarity),
                "similarity_stable": stable,
                "accepted": accepted,
                "near_threshold": near_threshold,
                "subnormal_guarded": guarded,
                "nonfinite_guarded": nonfinite,
                "decision_rule": rule,
                "metadata": candidate.metadata,
            }
        )
    if config.compare_strategy == "strict":
        prepared.sort(key=lambda item: (-item["similarity_stable"], item["candidate_id"]))
    else:
        prepared.sort(key=lambda item: (-item["similarity_stable"], item["candidate_id"]))
        stable_order: list[dict[str, Any]] = []
        start = 0
        while start < len(prepared):
            group_top = prepared[start]["similarity_stable"]
            end = start + 1
            while (
                end < len(prepared)
                and group_top - prepared[end]["similarity_stable"] <= config.tolerance
            ):
                end += 1
            group = prepared[start:end]
            group.sort(key=lambda item: item["candidate_id"])
            stable_order.extend(group)
            start = end
        prepared = stable_order
    for rank, item in enumerate(prepared, start=1):
        item["rank"] = rank
        item["selected_top_k"] = rank <= config.top_k
        item["trace"] = trace
    top_k = prepared[: config.top_k]
    accepted = [item for item in prepared if item["accepted"]]
    return {
        "strategy": config.compare_strategy,
        "threshold": config.threshold,
        "tolerance": config.tolerance,
        "top_k": config.top_k,
        "subnormal_floor": config.subnormal_floor,
        "accepted_any": bool(accepted),
        "best_accepted_candidate_id": accepted[0]["candidate_id"] if accepted else None,
        "subnormal_guard_count": sum(
            1 for item in prepared if item["subnormal_guarded"]
        ),
        "nonfinite_guard_count": sum(
            1 for item in prepared if item["nonfinite_guarded"]
        ),
        "ranked": prepared,
        "top_k_candidates": top_k,
        "trace": trace,
        "decision_is_repeat_stable": True,
        "stability_rule": (
            "cosine clamped to [-1,1], |x| below subnormal_floor treated as 0, "
            "tolerance uses an inclusive lower threshold band and candidate_id tie ordering"
        ),
    }


def adjudicate_clone_similarity(
    scores: Iterable[CloneCandidateScore],
    config: SimilarityConfig,
    *,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return rank_clone_scores(scores, config, trace=trace)
