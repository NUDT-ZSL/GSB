from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import digest_json, stable_float
from .config import ROUTE_ORDER, ConfigSnapshot
from .ingest import Document, PreparedCorpus, Query


@dataclass(frozen=True)
class RouteHit:
    doc_id: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "score": stable_float(self.score)}


def _sort_hits(hits: list[RouteHit], limit: int) -> list[RouteHit]:
    ordered = sorted(hits, key=lambda item: (-item.score, item.doc_id))
    return ordered[:limit]


def _filtered_documents(corpus: PreparedCorpus, query: Query) -> list[Document]:
    return [
        doc
        for doc in corpus.all_documents
        if corpus.matches_filter(doc, query.filters)
    ]


def _longest_common_prefix(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _lexical_score(
    corpus: PreparedCorpus, query: Query, doc: Document, route_config: dict[str, Any]
) -> float:
    term_tf = doc.term_tf
    k1 = float(route_config["k1"])
    b = float(route_config["b"])
    score = 0.0
    for term in query.terms:
        tf = term_tf.get(term)
        if tf is None:
            continue
        df = len(corpus.postings.get(term, {}))
        if df <= 0:
            continue
        n_docs = len(corpus.all_documents)
        idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
        denominator = tf + k1 * (
            1.0 - b + b * (doc.doc_len / corpus.avgdl)
        )
        score += idf * (tf * (k1 + 1.0)) / denominator
    return score


def recall_lexical(
    corpus: PreparedCorpus, query: Query, route_config: dict[str, Any]
) -> dict[str, Any]:
    hits: list[RouteHit] = []
    for doc in _filtered_documents(corpus, query):
        score = _lexical_score(corpus, query, doc, route_config)
        if score > 0.0:
            hits.append(RouteHit(doc.doc_id, score))
    selected = _sort_hits(hits, int(route_config["limit"]))
    return {
        "route": "lexical",
        "enabled": bool(route_config["enabled"]),
        "limit": int(route_config["limit"]),
        "candidates": [hit.to_dict() for hit in selected],
    }


def _prefix_score(
    query: Query, doc: Document, route_config: dict[str, Any]
) -> float:
    scale = float(route_config["scale"])
    minimum = int(route_config["min_prefix"])
    score = 0.0
    for query_term in query.terms:
        best = 0.0
        for item in doc.terms:
            prefix_len = _longest_common_prefix(query_term, item.term)
            if prefix_len < minimum:
                continue
            fraction = prefix_len / max(len(query_term), len(item.term))
            candidate = fraction * (1.0 + math.log1p(item.tf))
            if candidate > best:
                best = candidate
        score += best
    return score * scale


def recall_prefix(
    corpus: PreparedCorpus, query: Query, route_config: dict[str, Any]
) -> dict[str, Any]:
    hits: list[RouteHit] = []
    for doc in _filtered_documents(corpus, query):
        score = _prefix_score(query, doc, route_config)
        if score > 0.0:
            hits.append(RouteHit(doc.doc_id, score))
    selected = _sort_hits(hits, int(route_config["limit"]))
    return {
        "route": "prefix",
        "enabled": bool(route_config["enabled"]),
        "limit": int(route_config["limit"]),
        "candidates": [hit.to_dict() for hit in selected],
    }


def _category_popularity(corpus: PreparedCorpus) -> dict[str, float]:
    result: dict[str, float] = {}
    for category, doc_ids in corpus.category_docs.items():
        if not doc_ids:
            result[category] = 0.0
            continue
        total = math.fsum(
            corpus.doc_by_id[doc_id].quality_score for doc_id in doc_ids
        )
        result[category] = total / len(doc_ids)
    return result


def recall_category(
    corpus: PreparedCorpus, query: Query, route_config: dict[str, Any]
) -> dict[str, Any]:
    popularity = _category_popularity(corpus)
    scale = float(route_config["popularity_scale"])
    hits: list[RouteHit] = []
    for doc in _filtered_documents(corpus, query):
        score = doc.quality_score + scale * popularity[doc.category]
        hits.append(RouteHit(doc.doc_id, score))
    selected = _sort_hits(hits, int(route_config["limit"]))
    return {
        "route": "category",
        "enabled": bool(route_config["enabled"]),
        "limit": int(route_config["limit"]),
        "category_popularity": {
            category: stable_float(popularity[category])
            for category in sorted(popularity)
        },
        "candidates": [hit.to_dict() for hit in selected],
    }


def _seeded_vector(namespace: str, *, dim: int, seed: int) -> np.ndarray:
    material = f"{seed}:{namespace}".encode("utf-8")
    raw = hashlib.sha256(material).digest()
    chunks: list[int] = []
    counter = 0
    while len(chunks) < dim:
        digest = hashlib.sha256(raw + counter.to_bytes(4, "big")).digest()
        chunks.extend(int.from_bytes(digest[index : index + 4], "big") for index in range(0, 32, 4))
        counter += 1
    values = np.asarray(
        [((value / (2**32 - 1)) * 2.0) - 1.0 for value in chunks[:dim]],
        dtype=np.float64,
    )
    norm = float(np.linalg.norm(values))
    if norm == 0.0:
        values[0] = 1.0
        norm = 1.0
    return values / norm


def document_vector(doc_id: str, *, dim: int, seed: int) -> np.ndarray:
    return _seeded_vector(f"doc:{doc_id}", dim=dim, seed=seed)


def query_vector(query: Query, *, dim: int, seed: int) -> np.ndarray:
    terms = "|".join(query.terms)
    return _seeded_vector(
        f"query:{query.query_id}:{terms}", dim=dim, seed=seed
    )


def recall_vector(
    corpus: PreparedCorpus, query: Query, route_config: dict[str, Any]
) -> dict[str, Any]:
    dim = int(route_config["dim"])
    seed = int(route_config["seed"])
    q_vector = query_vector(query, dim=dim, seed=seed)
    hits: list[RouteHit] = []
    for doc in _filtered_documents(corpus, query):
        d_vector = document_vector(doc.doc_id, dim=dim, seed=seed)
        score = float(np.dot(q_vector, d_vector))
        hits.append(RouteHit(doc.doc_id, score))
    selected = _sort_hits(hits, int(route_config["limit"]))
    return {
        "route": "vector",
        "enabled": bool(route_config["enabled"]),
        "limit": int(route_config["limit"]),
        "dim": dim,
        "seed": seed,
        "candidates": [hit.to_dict() for hit in selected],
    }


RECALLERS = {
    "lexical": recall_lexical,
    "prefix": recall_prefix,
    "category": recall_category,
    "vector": recall_vector,
}


def run_recall(
    corpus: PreparedCorpus,
    query: Query,
    config: ConfigSnapshot,
) -> dict[str, Any]:
    routes: dict[str, Any] = {}
    for name in ROUTE_ORDER:
        route_config = config.recalls[name]
        if route_config["enabled"]:
            routes[name] = RECALLERS[name](corpus, query, route_config)
        else:
            routes[name] = {
                "route": name,
                "enabled": False,
                "limit": int(route_config["limit"]),
                "candidates": [],
            }
    return routes


def _relevant_lexical_docs(
    corpus: PreparedCorpus, query: Query
) -> list[dict[str, Any]]:
    query_terms = set(query.terms)
    payload: list[dict[str, Any]] = []
    for doc in corpus.all_documents:
        if not corpus.matches_filter(doc, query.filters):
            continue
        matching = {
            item.term: item.tf for item in doc.terms if item.term in query_terms
        }
        if not matching:
            continue
        payload.append(
            {
                "doc_id": doc.doc_id,
                "doc_len": doc.doc_len,
                "category": doc.category,
                "quality_score": stable_float(doc.quality_score),
                "term_tf": matching,
            }
        )
    return payload


def _relevant_prefix_docs(
    corpus: PreparedCorpus, query: Query, minimum: int
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for doc in corpus.all_documents:
        if not corpus.matches_filter(doc, query.filters):
            continue
        matches: list[dict[str, Any]] = []
        for item in doc.terms:
            best = 0
            for query_term in query.terms:
                best = max(best, _longest_common_prefix(query_term, item.term))
            if best >= minimum:
                matches.append({"term": item.term, "tf": item.tf, "lcp": best})
        if matches:
            payload.append(
                {
                    "doc_id": doc.doc_id,
                    "doc_len": doc.doc_len,
                    "category": doc.category,
                    "quality_score": stable_float(doc.quality_score),
                    "matches": matches,
                }
            )
    return payload


def _relevant_category_docs(
    corpus: PreparedCorpus, query: Query
) -> list[dict[str, Any]]:
    payload = []
    for doc in corpus.all_documents:
        if not corpus.matches_filter(doc, query.filters):
            continue
        payload.append(
            {
                "doc_id": doc.doc_id,
                "category": doc.category,
                "quality_score": stable_float(doc.quality_score),
                "doc_len": doc.doc_len,
            }
        )
    return payload


def _relevant_vector_docs(
    corpus: PreparedCorpus, query: Query
) -> list[dict[str, Any]]:
    payload = []
    for doc in corpus.all_documents:
        if not corpus.matches_filter(doc, query.filters):
            continue
        payload.append(
            {
                "doc_id": doc.doc_id,
                "category": doc.category,
                "quality_score": stable_float(doc.quality_score),
            }
        )
    return payload


def route_dependency(
    corpus: PreparedCorpus,
    query: Query,
    route: str,
    route_config: dict[str, Any],
) -> dict[str, Any]:
    if route == "lexical":
        return {
            "route": route,
            "query_terms": list(query.terms),
            "filters": query.filters,
            "corpus_stats": {
                "document_count": len(corpus.all_documents),
                "total_terms": corpus.total_terms,
                "average_doc_len": stable_float(corpus.avgdl),
            },
            "documents": _relevant_lexical_docs(corpus, query),
        }
    if route == "prefix":
        return {
            "route": route,
            "query_terms": list(query.terms),
            "filters": query.filters,
            "min_prefix": int(route_config["min_prefix"]),
            "documents": _relevant_prefix_docs(
                corpus, query, int(route_config["min_prefix"])
            ),
        }
    if route == "category":
        popularity = _category_popularity(corpus)
        return {
            "route": route,
            "filters": query.filters,
            "category_popularity": {
                category: stable_float(popularity[category])
                for category in sorted(popularity)
            },
            "documents": _relevant_category_docs(corpus, query),
        }
    if route == "vector":
        return {
            "route": route,
            "filters": query.filters,
            "dim": int(route_config["dim"]),
            "seed": int(route_config["seed"]),
            "documents": _relevant_vector_docs(corpus, query),
        }
    raise ValueError(f"unknown route: {route}")


def route_cache_key(
    corpus: PreparedCorpus,
    query: Query,
    route: str,
    route_config: dict[str, Any],
    *,
    query_content_hash: str,
) -> tuple[str, dict[str, Any]]:
    dependency = route_dependency(corpus, query, route, route_config)
    config_params = dict(route_config)
    config_params.pop("weight", None)
    key = digest_json(
        {
            "schema": "route_cache.v1",
            "route": route,
            "route_config": config_params,
            "query_content_hash": query_content_hash,
            "dependency": dependency,
        }
    )
    return key, dependency


def route_result_hash(route_result: dict[str, Any]) -> str:
    return digest_json(route_result)


def route_cache_plan(
    corpus: PreparedCorpus, query: Query, config: ConfigSnapshot
) -> dict[str, Any]:
    query_hash = digest_json(query.content_dict())
    routes: dict[str, Any] = {}
    for name in ROUTE_ORDER:
        route_config = config.recalls[name]
        key, dependency = route_cache_key(
            corpus,
            query,
            name,
            route_config,
            query_content_hash=query_hash,
        )
        routes[name] = {
            "cache_key": key,
            "dependency_hash": digest_json(dependency),
            "enabled": route_config["enabled"],
        }
    return {
        "query_content_hash": query_hash,
        "routes": routes,
        "dependency_hash": digest_json(
            {name: routes[name]["dependency_hash"] for name in ROUTE_ORDER}
        ),
    }
