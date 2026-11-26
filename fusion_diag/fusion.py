from __future__ import annotations

import math
from typing import Any

from .canonical import digest_json, stable_float, stable_sum
from .config import ROUTE_ORDER, ConfigSnapshot
from .errors import CandidateDrop
from .ingest import Query


def _normalize_route(
    route: str,
    candidates: list[dict[str, Any]],
    *,
    method: str,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    scores = [stable_float(float(item["score"])) for item in candidates]
    normalized: list[float] = []
    if method == "minmax":
        low = min(scores)
        high = max(scores)
        span = high - low
        if span <= 2.2250738585072014e-308:
            normalized = [1.0 for _ in scores]
        else:
            normalized = [
                stable_float((score - low) / span) for score in scores
            ]
    elif method == "reciprocal_rank":
        normalized = [
            stable_float(1.0 / rank) for rank in range(1, len(scores) + 1)
        ]
    else:
        raise ValueError(f"unknown normalization: {method}")
    result = []
    for item, score, norm in zip(candidates, scores, normalized):
        result.append(
            {
                "doc_id": item["doc_id"],
                "raw_score": stable_float(score),
                "normalized_score": stable_float(norm),
                "route": route,
            }
        )
    return result


def _tolerance_topk(
    candidates: list[dict[str, Any]], top_k: int, tolerance: float
) -> list[dict[str, Any]]:
    ordered = sorted(
        candidates, key=lambda item: (-item["fusion_score"], item["doc_id"])
    )
    banded: list[dict[str, Any]] = []
    band: list[dict[str, Any]] = []
    anchor = 0.0
    for item in ordered:
        score = item["fusion_score"]
        if not band:
            band = [item]
            anchor = score
            continue
        if anchor - score <= tolerance + 2.2250738585072014e-308:
            band.append(item)
        else:
            banded.extend(
                sorted(band, key=lambda entry: entry["doc_id"])
            )
            band = [item]
            anchor = score
    if band:
        banded.extend(sorted(band, key=lambda entry: entry["doc_id"]))
    return banded[:top_k]


def _strict_topk(
    candidates: list[dict[str, Any]], top_k: int
) -> list[dict[str, Any]]:
    ordered = sorted(
        candidates, key=lambda item: (-item["fusion_score"], item["doc_id"])
    )
    return ordered[:top_k]


def align_candidates(
    query: Query,
    routes: dict[str, Any],
    config: ConfigSnapshot,
) -> dict[str, Any]:
    method = config.fusion["normalization"]
    normalized_by_route: dict[str, list[dict[str, Any]]] = {}
    for route in ROUTE_ORDER:
        result = routes[route]
        candidates = result.get("candidates", [])
        seen: set[str] = set()
        for candidate in candidates:
            doc_id = candidate.get("doc_id")
            if not isinstance(doc_id, str) or not doc_id:
                raise CandidateDrop("candidate is missing doc_id", doc_id=str(doc_id))
            if doc_id in seen:
                raise CandidateDrop(
                    f"duplicate candidate inside route {route}", doc_id=doc_id
                )
            seen.add(doc_id)
            score = candidate.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise CandidateDrop(
                    f"candidate in route {route} is missing score", doc_id=doc_id
                )
        normalized_by_route[route] = _normalize_route(
            route, candidates, method=method
        )
    aligned: dict[str, dict[str, Any]] = {}
    for route in ROUTE_ORDER:
        weight = stable_float(float(config.recalls[route]["weight"]))
        for item in normalized_by_route[route]:
            doc_id = item["doc_id"]
            contribution = stable_float(item["normalized_score"] * weight)
            source = {
                "route": route,
                "raw_score": item["raw_score"],
                "normalized_score": item["normalized_score"],
                "weight": weight,
                "contribution": contribution,
            }
            aligned.setdefault(doc_id, {"doc_id": doc_id, "sources": []})[
                "sources"
            ].append(source)
    candidates: list[dict[str, Any]] = []
    for doc_id in sorted(aligned):
        entry = aligned[doc_id]
        sources = sorted(
            entry["sources"],
            key=lambda item: ROUTE_ORDER.index(item["route"]),
        )
        if not sources:
            raise CandidateDrop(
                "aligned candidate has no recall sources", doc_id=doc_id
            )
        present_routes = [source["route"] for source in sources]
        if len(set(present_routes)) != len(present_routes):
            raise CandidateDrop(
                "aligned candidate has duplicate recall sources", doc_id=doc_id
            )
        fused_score = stable_sum(
            [float(source["contribution"]) for source in sources]
        )
        if config.fusion["dedup_policy"] == "first_hit":
            retained = sources[0]
        else:
            retained = sorted(
                sources,
                key=lambda item: (
                    -float(item["contribution"]),
                    ROUTE_ORDER.index(item["route"]),
                ),
            )[0]
        discarded = [
            {
                **source,
                "discard_reason": "dedup_representative_not_retained",
            }
            for source in sources
            if source is not retained
        ]
        recomputed = stable_sum(
            [float(source["contribution"]) for source in sources]
        )
        if recomputed != fused_score:
            raise CandidateDrop(
                "fusion score cannot be reproduced from contribution details",
                doc_id=doc_id,
            )
        candidates.append(
            {
                "doc_id": doc_id,
                "fusion_score": fused_score,
                "sources": sources,
                "dedup": {
                    "policy": config.fusion["dedup_policy"],
                    "retained_source": retained["route"],
                    "discarded_route_hits": discarded,
                },
            }
        )
    candidates.sort(key=lambda item: (-item["fusion_score"], item["doc_id"]))
    merged_sources = sum(
        max(0, len(item["sources"]) - 1) for item in candidates
    )
    return {
        "normalization": method,
        "candidates": candidates,
        "normalized_route_candidate_counts": {
            route: len(normalized_by_route[route]) for route in ROUTE_ORDER
        },
        "trace": {
            "merged_source_count": merged_sources,
            "normalization_calculations": sum(
                len(normalized_by_route[route]) for route in ROUTE_ORDER
            ),
            "fusion_weight_count": sum(
                1 for route in ROUTE_ORDER if config.recalls[route]["enabled"]
            ),
        },
    }


def finalize_fusion(
    query: Query,
    alignment: dict[str, Any],
    config: ConfigSnapshot,
) -> dict[str, Any]:
    candidates = list(alignment["candidates"])
    budget = int(config.fusion["candidate_budget"])
    budget_dropped = candidates[budget:]
    candidates = candidates[:budget]
    if config.fusion["topk_compare"] == "strict":
        ranked = _strict_topk(candidates, query.top_k)
    else:
        ranked = _tolerance_topk(
            candidates, query.top_k, float(config.fusion["tolerance"])
        )
    top_k = [
        {
            "rank": rank,
            "doc_id": item["doc_id"],
            "score": item["fusion_score"],
        }
        for rank, item in enumerate(ranked, start=1)
    ]
    return {
        "normalization": alignment["normalization"],
        "candidates": candidates,
        "top_k": top_k,
        "trace": {
            "route_candidate_counts": alignment[
                "normalized_route_candidate_counts"
            ],
            "merged_source_count": alignment["trace"]["merged_source_count"],
            "deduplicated_candidate_count": len(candidates),
            "budget": budget,
            "budget_dropped": budget_dropped,
            "normalization_calculations": alignment["trace"][
                "normalization_calculations"
            ],
            "fusion_weight_count": alignment["trace"]["fusion_weight_count"],
        },
    }


def fuse(
    query: Query,
    routes: dict[str, Any],
    config: ConfigSnapshot,
) -> dict[str, Any]:
    alignment = align_candidates(query, routes, config)
    return finalize_fusion(query, alignment, config)


def fusion_cache_key(
    *,
    documents_content_hash: str,
    query_content_hash: str,
    config_digest: str,
    upstream_node_hash: str,
) -> str:
    return digest_json(
        {
            "schema": "fusion_cache.v1",
            "documents_content_hash": documents_content_hash,
            "query_content_hash": query_content_hash,
            "config_parameter_version": config_digest,
            "upstream_node_hash": upstream_node_hash,
        }
    )


def validate_fusion_recomputation(
    candidate: dict[str, Any], *, tolerance: float = 1e-12
) -> bool:
    total = stable_sum(
        [float(source["contribution"]) for source in candidate["sources"]]
    )
    return math.isclose(
        total, float(candidate["fusion_score"]), rel_tol=0.0, abs_tol=tolerance
    )
