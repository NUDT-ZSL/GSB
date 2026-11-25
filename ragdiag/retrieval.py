"""Deterministic three-route evidence retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, format_float, sha256_bytes, sha256_value
from .errors import E_FORMAT_CORRUPT, WorkbenchError


ROUTE_ORDER = ("inverted", "prefix", "vector")
TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


def load_corpus_documents(records: list[Any]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, record in enumerate(records, start=1):
        if isinstance(record, str):
            line = record
        else:
            line = ""
        if isinstance(record, dict):
            document = record
        else:
            try:
                document = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    "evidence corpus JSON is corrupt",
                    line=line_no,
                    error=str(exc),
                ) from exc
        if not line.strip():
            if isinstance(record, str):
                continue
        doc_id = document.get("doc_id")
        text = document.get("text")
        tags = document.get("tags", [])
        if not isinstance(doc_id, str) or not doc_id or not isinstance(text, str):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "evidence corpus document requires doc_id and text",
                line=line_no,
            )
        if doc_id in seen:
            raise WorkbenchError(
                E_FORMAT_CORRUPT, "duplicate evidence doc_id", doc_id=doc_id
            )
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise WorkbenchError(
                E_FORMAT_CORRUPT, "evidence tags must be strings", doc_id=doc_id
            )
        seen.add(doc_id)
        documents.append(
            {"doc_id": doc_id, "text": text, "tags": sorted(tags)}
        )
    documents.sort(key=lambda item: item["doc_id"])
    if not documents:
        raise WorkbenchError(E_FORMAT_CORRUPT, "evidence corpus is empty")
    return documents


def load_corpus(path: str | Path) -> list[dict[str, Any]]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkbenchError(
            E_FORMAT_CORRUPT, "evidence corpus cannot be loaded", error=str(exc)
        ) from exc
    return load_corpus_documents(lines)


def corpus_hash(documents: list[dict[str, Any]]) -> str:
    return sha256_value(documents)


def _unit_vector(values: list[float]) -> list[float]:
    norm = math.sqrt(math.fsum(value * value for value in values))
    if norm == 0.0:
        return [0.0 for _ in values]
    return [value / norm for value in values]


def _hash_vector(label: str, dimension: int) -> list[float]:
    digest = hashlib.shake_256(label.encode("utf-8")).digest(dimension * 2)
    values = [
        int.from_bytes(digest[index : index + 2], "big") / 32768.0 - 1.0
        for index in range(0, dimension * 2, 2)
    ]
    return _unit_vector(values)


def _document_vector(tokens: list[str], seed: int, dimension: int) -> list[float]:
    accumulators = [0.0] * dimension
    for token in sorted(tokens):
        token_vector = _hash_vector(f"token|{seed}|{token}", dimension)
        for index, component in enumerate(token_vector):
            accumulators[index] += component
    return _unit_vector(accumulators)


def _dot(left: list[float], right: list[float]) -> float:
    return math.fsum(a * b for a, b in zip(left, right))


def _lsh_bucket(vector: list[float], seed: int, bits: int) -> str:
    signs = []
    for bit in range(bits):
        plane = _hash_vector(f"plane|{seed}|{bit}", len(vector))
        signs.append("1" if _dot(vector, plane) >= 0.0 else "0")
    return "".join(signs)


def _hamming(left: str, right: str) -> int:
    return sum(a != b for a, b in zip(left, right))


def _index_build_config(config: dict[str, Any]) -> dict[str, Any]:
    retrieval = config["retrieval"]
    precision = config["precision"]
    return {
        "seed": int(config.get("seed", 0)),
        "prefix_expansion": int(retrieval.get("prefix_expansion", 3)),
        "vector_dim": int(retrieval.get("vector_dim", 24)),
        "vector_lsh_bits": int(retrieval.get("vector_lsh_bits", 8)),
        "score_decimals": int(precision.get("score_decimals", 10)),
        "rounding_mode": str(precision.get("rounding_mode", "half_even")),
        "subnormal_protection": bool(
            precision.get("subnormal_protection", True)
        ),
    }


def index_key_material(documents: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": "retrieval_index",
        "data": corpus_hash(documents),
        "config": sha256_value(_index_build_config(config)),
    }


def build_index(documents: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    retrieval = config["retrieval"]
    seed = int(config.get("seed", 0))
    dimension = int(retrieval.get("vector_dim", 24))
    bits = int(retrieval.get("vector_lsh_bits", 8))
    docs = []
    inverted: dict[str, list[str]] = {}
    df: dict[str, int] = {}
    vocab: set[str] = set()
    for document in documents:
        tokens = tokenize(document["text"])
        ordered_tokens = sorted(tokens)
        vocab.update(ordered_tokens)
        term_counts: dict[str, int] = {}
        for token in ordered_tokens:
            term_counts[token] = term_counts.get(token, 0) + 1
        for term in sorted(term_counts):
            inverted.setdefault(term, []).append(document["doc_id"])
            df[term] = df.get(term, 0) + 1
        vector = _document_vector(tokens, seed, dimension)
        docs.append(
            {
                "doc_id": document["doc_id"],
                "text": document["text"],
                "tags": document["tags"],
                "tokens": ordered_tokens,
                "term_counts": {
                    term: term_counts[term] for term in sorted(term_counts)
                },
                "vector": vector,
                "bucket": _lsh_bucket(vector, seed, bits),
            }
        )
    for term in inverted:
        inverted[term].sort()
    doc_count = len(docs)
    idf = {
        term: math.log((doc_count + 1.0) / (df[term] + 1.0)) + 1.0
        for term in sorted(df)
    }
    index = {
        "docs": docs,
        "inverted": {term: inverted[term] for term in sorted(inverted)},
        "df": {term: df[term] for term in sorted(df)},
        "idf": idf,
        "vocab": sorted(vocab),
        "corpus_hash": corpus_hash(documents),
        "build_config": _index_build_config(config),
    }
    index["index_hash"] = sha256_value(
        {
            "corpus_hash": index["corpus_hash"],
            "build_config": index["build_config"],
        }
    )
    return index


def retrieval_config_material(config: dict[str, Any]) -> dict[str, Any]:
    retrieval = config["retrieval"]
    precision = config["precision"]
    return {
        "weights": {
            route: float(retrieval["weights"][route]) for route in ROUTE_ORDER
        },
        "top_k": int(retrieval.get("top_k", 8)),
        "candidate_limit": int(retrieval.get("candidate_limit", 8)),
        "prefix_expansion": int(retrieval.get("prefix_expansion", 3)),
        "vector_dim": int(retrieval.get("vector_dim", 24)),
        "vector_lsh_bits": int(retrieval.get("vector_lsh_bits", 8)),
        "vector_probe_buckets": int(
            retrieval.get("vector_probe_buckets", 2)
        ),
        "use_turn_context": bool(retrieval.get("use_turn_context", True)),
        "seed": int(config.get("seed", 0)),
        "precision": {
            "score_decimals": int(precision.get("score_decimals", 10)),
            "rounding_mode": str(
                precision.get("rounding_mode", "half_even")
            ),
            "subnormal_protection": bool(
                precision.get("subnormal_protection", True)
            ),
        },
    }


def retrieval_config_hash(config: dict[str, Any]) -> str:
    return sha256_value(retrieval_config_material(config))


def _route_rank(
    raw_scores: dict[str, float],
    route: str,
    weight: float,
    top_k: int,
    precision: dict[str, Any],
) -> list[dict[str, Any]]:
    ordered = sorted(
        ((doc_id, score) for doc_id, score in raw_scores.items() if score > 0.0),
        key=lambda item: (-item[1], item[0]),
    )[:top_k]
    output = []
    for doc_id, raw in ordered:
        raw_q = format_float(raw, precision)
        weighted_q = format_float(raw_q * weight, precision)
        output.append(
            {
                "doc_id": doc_id,
                "raw_score": raw_q,
                "weight": format_float(weight, precision),
                "weighted": weighted_q,
            }
        )
    return output


def _inverted_scores(
    query_tokens: list[str], index: dict[str, Any]
) -> dict[str, float]:
    accumulators: dict[str, list[float]] = {}
    for term in sorted(set(query_tokens)):
        if term not in index["inverted"]:
            continue
        idf = index["idf"][term]
        for doc_id in index["inverted"][term]:
            doc = next(
                doc for doc in index["docs"] if doc["doc_id"] == doc_id
            )
            term_frequency = doc["term_counts"][term]
            accumulators.setdefault(doc_id, []).append(
                idf * term_frequency / (term_frequency + 1.0)
            )
    return {
        doc_id: math.fsum(parts) for doc_id, parts in sorted(accumulators.items())
    }


def _prefix_scores(
    query_tokens: list[str], index: dict[str, Any], expansion_limit: int
) -> dict[str, float]:
    expansions: list[str] = []
    for query_token in sorted(set(query_tokens)):
        matches = [
            term
            for term in index["vocab"]
            if term != query_token and term.startswith(query_token)
        ]
        expansions.extend(matches[:expansion_limit])
    accumulators: dict[str, list[float]] = {}
    doc_lookup = {doc["doc_id"]: doc for doc in index["docs"]}
    for term in sorted(set(expansions)):
        for doc_id in index["inverted"].get(term, []):
            doc = doc_lookup[doc_id]
            term_frequency = doc["term_counts"][term]
            contribution = 0.6 * index["idf"][term] * (
                term_frequency / (term_frequency + 1.0)
            )
            accumulators.setdefault(doc_id, []).append(contribution)
    return {
        doc_id: math.fsum(parts) for doc_id, parts in sorted(accumulators.items())
    }


def _vector_scores(
    query_tokens: list[str],
    index: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, float], list[str]]:
    retrieval = config["retrieval"]
    seed = int(config.get("seed", 0))
    dimension = int(retrieval.get("vector_dim", 24))
    bits = int(retrieval.get("vector_lsh_bits", 8))
    probe_buckets = max(0, int(retrieval.get("vector_probe_buckets", 2)))
    query_vector = _document_vector(query_tokens, seed, dimension)
    query_bucket = _lsh_bucket(query_vector, seed, bits)
    grouped: dict[str, list[str]] = {}
    for doc in index["docs"]:
        grouped.setdefault(doc["bucket"], []).append(doc["doc_id"])
    ordered_buckets = sorted(
        grouped,
        key=lambda bucket: (_hamming(query_bucket, bucket), bucket),
    )
    selected_buckets = ordered_buckets[:probe_buckets]
    selected_docs = {
        doc_id
        for bucket in selected_buckets
        for doc_id in grouped[bucket]
    }
    scores: dict[str, float] = {}
    for doc in index["docs"]:
        if doc["doc_id"] not in selected_docs:
            continue
        score = max(0.0, _dot(query_vector, doc["vector"]))
        if score > 0.0:
            scores[doc["doc_id"]] = score
    return scores, selected_buckets


def retrieve(
    query_text: str,
    config: dict[str, Any],
    index: dict[str, Any],
    context_text: str = "",
) -> dict[str, Any]:
    retrieval = config["retrieval"]
    precision = config["precision"]
    top_k = int(retrieval.get("top_k", 8))
    candidate_limit = int(retrieval.get("candidate_limit", top_k))
    use_context = bool(retrieval.get("use_turn_context", True))
    effective_query = query_text
    if use_context and context_text:
        effective_query = f"{query_text} {context_text[:320]}"
    query_tokens = tokenize(effective_query)
    inverted_scores = _inverted_scores(query_tokens, index)
    prefix_scores = _prefix_scores(
        query_tokens,
        index,
        int(retrieval.get("prefix_expansion", 3)),
    )
    vector_scores, probed_buckets = _vector_scores(
        query_tokens, index, config
    )
    route_scores = {
        "inverted": inverted_scores,
        "prefix": prefix_scores,
        "vector": vector_scores,
    }
    weights = {
        route: float(retrieval["weights"][route]) for route in ROUTE_ORDER
    }
    route_results = {
        route: _route_rank(
            route_scores[route],
            route,
            weights[route],
            top_k,
            precision,
        )
        for route in ROUTE_ORDER
    }
    contributions: dict[str, list[dict[str, Any]]] = {}
    for route in ROUTE_ORDER:
        for row in route_results[route]:
            contributions.setdefault(row["doc_id"], []).append(
                {
                    "route": route,
                    "raw_score": row["raw_score"],
                    "weight": row["weight"],
                    "weighted": row["weighted"],
                }
            )
    candidates = []
    for doc_id, details in contributions.items():
        score = format_float(
            math.fsum(item["weighted"] for item in details), precision
        )
        candidates.append(
            {
                "doc_id": doc_id,
                "score": score,
                "contributions": details,
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["doc_id"]))
    candidates = candidates[:candidate_limit]
    artifact = {
        "query_text": query_text,
        "effective_query": effective_query,
        "context_used": bool(use_context and context_text),
        "index_hash": index["index_hash"],
        "routes": {
            route: {
                "matched_documents": len(route_scores[route]),
                "kept": len(route_results[route]),
                "weight": format_float(weights[route], precision),
            }
            for route in ROUTE_ORDER
        },
        "probed_vector_buckets": probed_buckets,
        "candidates": candidates,
    }
    artifact["retrieval_hash"] = sha256_value(artifact)
    return artifact


def retrieval_bytes(artifact: dict[str, Any]) -> bytes:
    return canonical_bytes(artifact)

