from __future__ import annotations

import hashlib
import math
from typing import Any

import numpy as np

from .config import PipelineConfig
from .models import Document
from .util import q6

ROUTE_ORDER = ("inverted", "category_popular", "vector_approx")
VECTOR_DIM = 48


def normalized_route_order(route_names: list[str]) -> list[str]:
    order = {name: index for index, name in enumerate(ROUTE_ORDER)}
    return sorted(route_names, key=lambda name: (order.get(name, len(order)), name))


def _query_weights(normalized: dict[str, Any]) -> dict[str, float]:
    return {
        str(item["term"]): float(item["weight"])
        for item in normalized["tokens"]
    }


def _term_vector(term: str, seed: int) -> tuple[int, float, int, float]:
    digest = hashlib.blake2b(
        f"{seed}:{term}".encode("utf-8"),
        digest_size=32,
    ).digest()
    first = int.from_bytes(digest[0:8], "big")
    second = int.from_bytes(digest[8:16], "big")
    third = int.from_bytes(digest[16:24], "big")
    index_a = first % VECTOR_DIM
    index_b = second % VECTOR_DIM
    sign_a = 1.0 if third & 1 else -1.0
    sign_b = -sign_a
    return index_a, sign_a, index_b, sign_b


def deterministic_vector(weights: dict[str, float], seed: int) -> np.ndarray:
    vector = np.zeros(VECTOR_DIM, dtype=np.float64)
    for term in sorted(weights):
        weight = float(weights[term])
        index_a, sign_a, index_b, sign_b = _term_vector(term, seed)
        vector[index_a] += sign_a * weight
        vector[index_b] += sign_b * weight
    norm = float(np.linalg.norm(vector))
    if norm > 0:
        vector /= norm
    return vector


def inverted_score(document: Document, normalized: dict[str, Any]) -> float:
    document_terms = {str(key).casefold(): int(value) for key, value in document.terms.items()}
    total = 0.0
    for term, query_weight in sorted(_query_weights(normalized).items()):
        count = document_terms.get(term)
        if count:
            total += query_weight * (1.0 + math.log1p(count))
    return q6(total)


def category_popular_score(document: Document, normalized: dict[str, Any]) -> float:
    hint_category = normalized.get("hint_category")
    if not hint_category or document.category != hint_category:
        return 0.0
    document_terms = {str(key).casefold() for key in document.terms}
    matched = sum(1 for term in _query_weights(normalized) if term in document_terms)
    return q6(float(document.popularity) * (1.0 + 0.1 * matched))


def vector_score(document: Document, normalized: dict[str, Any], config: PipelineConfig) -> float:
    doc_weights = {
        str(term).casefold(): 1.0 + math.log1p(int(count))
        for term, count in document.terms.items()
    }
    doc_vector = deterministic_vector(doc_weights, config.seed)
    query_vector = deterministic_vector(_query_weights(normalized), config.seed)
    return q6(float(np.dot(doc_vector, query_vector)))


def score_document_route(
    route: str,
    document: Document,
    normalized: dict[str, Any],
    config: PipelineConfig,
) -> float:
    if route == "inverted":
        return inverted_score(document, normalized)
    if route == "category_popular":
        return category_popular_score(document, normalized)
    if route == "vector_approx":
        return vector_score(document, normalized, config)
    raise ValueError(f"unknown route: {route}")


def merge_recall(
    route_scores: dict[str, dict[str, float]],
    documents: dict[str, Document],
    normalized: dict[str, Any],
    config: PipelineConfig,
) -> dict[str, Any]:
    route_weights = config.route_weights or {}
    candidates: dict[str, dict[str, Any]] = {}
    for route in ROUTE_ORDER:
        per_doc = route_scores.get(route, {})
        ranked = sorted(per_doc.items(), key=lambda item: (-item[1], item[0]))
        if route == "vector_approx":
            ranked = ranked[: config.vector_top_k]
        for doc_id, raw_score in ranked:
            if raw_score == 0:
                continue
            candidate = candidates.setdefault(
                doc_id,
                {"doc_id": doc_id, "final_score": 0.0, "routes": []},
            )
            candidate["routes"].append({"route": route, "raw_score": q6(raw_score)})
            candidate["final_score"] = q6(
                candidate["final_score"]
                + q6(raw_score) * float(route_weights.get(route, 0.0))
            )
    for candidate in candidates.values():
        candidate["routes"].sort(key=lambda item: ROUTE_ORDER.index(item["route"]))
        candidate["doc_version"] = documents[candidate["doc_id"]].doc_version
    ordered = sorted(candidates.values(), key=lambda item: (-item["final_score"], item["doc_id"]))
    selected = ordered[: config.recall_top_k]
    return {
        "conv_id": normalized["conv_id"],
        "turn_no": normalized["turn_no"],
        "config_hash": config.content_hash,
        "routes": {
            route: [
                {"doc_id": doc_id, "raw_score": q6(score)}
                for doc_id, score in sorted(
                    route_scores.get(route, {}).items(),
                    key=lambda item: (-item[1], item[0]),
                )
                if score != 0
            ]
            for route in ROUTE_ORDER
        },
        "candidates": selected,
        "all_candidates": ordered,
        "top_doc_ids": [item["doc_id"] for item in selected],
    }


def recall(
    documents: dict[str, Document],
    normalized: dict[str, Any],
    config: PipelineConfig,
    route_scores: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    scores = route_scores or {
        route: {
            doc_id: score_document_route(route, document, normalized, config)
            for doc_id, document in sorted(documents.items())
        }
        for route in ROUTE_ORDER
    }
    return merge_recall(scores, documents, normalized, config)
