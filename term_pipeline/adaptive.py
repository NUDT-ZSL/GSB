"""Deterministic scale-based exact vs restricted-approximate selection."""

from __future__ import annotations

from typing import Any

from .features import document_feature_counts
from .util import rounded


def choose_path(
    documents: list[dict[str, Any]],
    scored_documents: list[dict[str, Any]],
    documents_to_candidates: dict[str, int],
    features: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    adaptive = config["adaptive"]
    doc_count = len(documents)
    shard_count = len({doc["shard_id"] for doc in documents})
    candidate_counts = [
        documents_to_candidates.get(doc["doc_id"], 0) for doc in documents
    ]
    feature_counts = document_feature_counts(documents, features)
    avg_candidates = (
        sum(candidate_counts) / doc_count if doc_count else 0.0
    )
    avg_features = (
        sum(feature_counts.get(doc["doc_id"], 0) for doc in documents) / doc_count
        if doc_count
        else 0.0
    )
    max_candidates = max(candidate_counts, default=0)
    memory_headroom = float(adaptive["resident_memory_headroom_mb"])

    exact_comparisons = 0
    approximate_comparisons = 0
    for doc in documents:
        candidate_count = documents_to_candidates.get(doc["doc_id"], 0)
        feature_count = max(1, feature_counts.get(doc["doc_id"], 0))
        exact_comparisons += candidate_count * feature_count
        approximate_comparisons += min(
            candidate_count,
            int(adaptive["approx_max_candidates_per_doc"]),
        ) * min(
            feature_count,
            int(adaptive["approx_max_features_per_candidate"]),
        )

    reasons: list[str] = []
    if shard_count > adaptive["exact_max_shards"]:
        reasons.append("SHARD_COUNT_EXCEEDS_EXACT_LIMIT")
    if max_candidates > adaptive["exact_max_candidates_per_doc"]:
        reasons.append("CANDIDATES_PER_DOC_EXCEED_EXACT_LIMIT")
    if avg_features > adaptive["exact_max_avg_features"]:
        reasons.append("AVG_FEATURES_EXCEED_EXACT_LIMIT")
    if memory_headroom < adaptive["exact_min_memory_headroom_mb"]:
        reasons.append("MEMORY_HEADROOM_BELOW_EXACT_LIMIT")
    selected_path = "approximate" if reasons else "exact"

    return {
        "observations": {
            "shard_count": shard_count,
            "doc_count": doc_count,
            "max_candidates_per_doc": max_candidates,
            "avg_candidates_per_doc": rounded(avg_candidates, 12),
            "avg_context_features": rounded(avg_features, 12),
            "resident_memory_headroom_mb": memory_headroom,
        },
        "thresholds": {
            "exact_max_shards": adaptive["exact_max_shards"],
            "exact_max_candidates_per_doc": adaptive[
                "exact_max_candidates_per_doc"
            ],
            "exact_max_avg_features": adaptive["exact_max_avg_features"],
            "exact_min_memory_headroom_mb": adaptive[
                "exact_min_memory_headroom_mb"
            ],
        },
        "comparison_estimates": {
            "exact": exact_comparisons,
            "restricted_approximate": approximate_comparisons,
        },
        "selected_path": selected_path,
        "selected_candidate_comparisons": (
            exact_comparisons if selected_path == "exact" else approximate_comparisons
        ),
        "reason_codes": reasons or ["ALL_EXACT_LIMITS_SATISFIED"],
        "coverage": {
            "doc_count": doc_count,
            "covered_doc_ids": [doc["doc_id"] for doc in documents],
            "all_documents_covered": doc_count == len(documents),
        },
        "approximate_limits": {
            "max_candidates_per_doc": int(
                adaptive["approx_max_candidates_per_doc"]
            ),
            "max_features_per_candidate": int(
                adaptive["approx_max_features_per_candidate"]
            ),
        },
    }


def restricted_candidates(
    scored_candidates: list[dict[str, Any]],
    path_decision: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    if path_decision["selected_path"] == "exact":
        return list(scored_candidates), []
    limit = int(path_decision["approximate_limits"]["max_candidates_per_doc"])
    ordered = sorted(
        scored_candidates,
        key=lambda candidate: (
            -float(candidate["confidence"]),
            candidate["start"],
            candidate["end"],
            candidate["candidate_id"],
        ),
    )
    kept = ordered[:limit]
    pruned = ordered[limit:]
    kept.sort(key=lambda candidate: (candidate["start"], candidate["end"]))
    return kept, sorted(candidate["candidate_id"] for candidate in pruned)

