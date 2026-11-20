"""Bridge-confidence-aware BM25 retrieval with auditable contributions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .data import Query
from .errors import format_corrupt, resource_limit
from .util import round_score, stable_json_bytes

PartitionLoader = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class RetrievalConfig:
    merge_policy: str = "sum"
    k1: float = 1.2
    b: float = 0.75
    max_postings: int = 1_000_000
    max_resident_bytes: int = 64 * 1024 * 1024

    def validate(self) -> None:
        if self.merge_policy not in {"sum", "max"}:
            raise format_corrupt(
                "unsupported contribution merge policy",
                merge_policy=self.merge_policy,
            )
        if self.k1 <= 0 or not 0 <= self.b <= 1:
            raise format_corrupt(
                "BM25 parameters are invalid",
                k1=self.k1,
                b=self.b,
            )
        if self.max_postings < 1 or self.max_resident_bytes < 1:
            raise format_corrupt("resource limits must be positive")


def search(
    query: Query,
    candidates: Sequence[Mapping[str, Any]],
    *,
    partition_langs: Sequence[str],
    partition_loader: PartitionLoader,
    total_postings: int,
    total_decoded_bytes: int,
    index_version: int,
    rule_version: int,
    rule_checksum: str,
    bridge_version: int,
    bridge_checksum: str,
    config: RetrievalConfig | None = None,
) -> dict[str, Any]:
    cfg = config or RetrievalConfig()
    cfg.validate()
    if total_postings > cfg.max_postings:
        raise resource_limit(
            "posting budget exceeded",
            max_postings=cfg.max_postings,
            actual_postings=total_postings,
        )

    candidate_langs = {candidate["target_lang"] for candidate in candidates}
    needed_langs = sorted(set(partition_langs) & (candidate_langs | {query.query_lang}))
    degraded: list[str] = []
    streaming = total_decoded_bytes > cfg.max_resident_bytes
    if streaming:
        degraded.append("DEGRADED_STREAMING_PARTITION_SCORING")

    baseline_accumulator: dict[str, list[dict[str, Any]]] = {}
    rewritten_accumulator: dict[str, list[dict[str, Any]]] = {}

    cached: dict[str, Mapping[str, Any]] = {}
    if not streaming:
        for lang in needed_langs:
            cached[lang] = partition_loader(lang)

    for lang in needed_langs:
        partition = cached[lang] if not streaming else partition_loader(lang)
        if lang == query.query_lang:
            _score_baseline(
                partition,
                query,
                baseline_accumulator,
                k1=cfg.k1,
                b=cfg.b,
            )
        lang_candidates = [
            candidate
            for candidate in candidates
            if candidate["target_lang"] == lang
        ]
        if lang_candidates:
            _score_candidates(
                partition,
                lang_candidates,
                rewritten_accumulator,
                k1=cfg.k1,
                b=cfg.b,
            )

    baseline_results = _finalize(
        baseline_accumulator,
        merge_policy=cfg.merge_policy,
        top_k=query.top_k,
        detail_key="baseline_score",
    )
    rewritten_results = _finalize(
        rewritten_accumulator,
        merge_policy=cfg.merge_policy,
        top_k=query.top_k,
        detail_key="final_score",
    )

    baseline_by_doc = {
        result["doc_id"]: result["score"] for result in baseline_results
    }
    rewritten_by_doc = {
        result["doc_id"]: result["score"] for result in rewritten_results
    }
    spectrum_docs = sorted(set(baseline_by_doc) | set(rewritten_by_doc))
    delta_spectrum = [
        {
            "doc_id": doc_id,
            "baseline_score": round_score(baseline_by_doc.get(doc_id, 0.0)),
            "rewritten_score": round_score(rewritten_by_doc.get(doc_id, 0.0)),
            "delta": round_score(
                rewritten_by_doc.get(doc_id, 0.0)
                - baseline_by_doc.get(doc_id, 0.0)
            ),
            "baseline_rank": _rank_of(doc_id, baseline_results),
            "rewritten_rank": _rank_of(doc_id, rewritten_results),
        }
        for doc_id in spectrum_docs
    ]

    return {
        "query_id": query.query_id,
        "query_lang": query.query_lang,
        "top_k": query.top_k,
        "index_version": index_version,
        "rule_version": rule_version,
        "rule_checksum": rule_checksum,
        "bridge_version": bridge_version,
        "bridge_checksum": bridge_checksum,
        "merge_policy": cfg.merge_policy,
        "resource_mode": "streaming" if streaming else "resident",
        "degraded": sorted(degraded),
        "candidate_count": len(candidates),
        "baseline_results": baseline_results,
        "rewritten_results": rewritten_results,
        "delta_spectrum": delta_spectrum,
    }


def _score_baseline(
    partition: Mapping[str, Any],
    query: Query,
    accumulator: dict[str, list[dict[str, Any]]],
    *,
    k1: float,
    b: float,
) -> None:
    postings = partition["postings"]
    term_index = partition["term_index"]
    stats = partition["stats"]
    for query_term, query_frequency in sorted(query.terms.items()):
        indexes = term_index.get(query_term)
        if not indexes:
            continue
        idf, df = _idf(stats["doc_count"], len(indexes))
        for posting_index in indexes:
            posting = postings[posting_index]
            bm25 = _bm25(posting, stats["avg_doc_len"], idf, k1=k1, b=b)
            contribution = round_score(bm25 * query_frequency)
            accumulator.setdefault(posting["doc_id"], []).append(
                {
                    "term": query_term,
                    "query_freq": query_frequency,
                    "tf": posting["tf"],
                    "doc_len": posting["doc_len"],
                    "df": df,
                    "idf": idf,
                    "bm25": bm25,
                    "contribution": contribution,
                    "source": posting["source"],
                }
            )


def _score_candidates(
    partition: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    accumulator: dict[str, list[dict[str, Any]]],
    *,
    k1: float,
    b: float,
) -> None:
    postings = partition["postings"]
    term_index = partition["term_index"]
    stats = partition["stats"]
    for candidate in sorted(
        candidates,
        key=lambda item: (
            item["target_lang"],
            item["target_term"],
            tuple(item["rule_ids"]),
            tuple(entry["target_term"] for entry in item["path"]),
        ),
    ):
        indexes = term_index.get(candidate["target_term"])
        if not indexes:
            continue
        idf, df = _idf(stats["doc_count"], len(indexes))
        for posting_index in indexes:
            posting = postings[posting_index]
            bm25 = _bm25(posting, stats["avg_doc_len"], idf, k1=k1, b=b)
            contribution = round_score(
                bm25
                * candidate["confidence"]
                * candidate["source_query_freq"]
            )
            accumulator.setdefault(posting["doc_id"], []).append(
                {
                    "target_lang": candidate["target_lang"],
                    "target_term": candidate["target_term"],
                    "source_lang": candidate["source_lang"],
                    "source_term": candidate["source_term"],
                    "source_query_freq": candidate["source_query_freq"],
                    "rule_ids": list(candidate["rule_ids"]),
                    "bridge_confidence": candidate["bridge_confidence"],
                    "rule_weight": candidate["rule_weight"],
                    "confidence": candidate["confidence"],
                    "depth": candidate["depth"],
                    "path": candidate["path"],
                    "tf": posting["tf"],
                    "doc_len": posting["doc_len"],
                    "df": df,
                    "idf": idf,
                    "bm25": bm25,
                    "contribution": contribution,
                    "source": posting["source"],
                }
            )


def _idf(doc_count: int, document_frequency: int) -> tuple[float, int]:
    denominator = document_frequency + 0.5
    idf = float(
        np.log(
            1.0
            + (doc_count - document_frequency + 0.5) / denominator
        )
    )
    return round_score(idf), document_frequency


def _bm25(
    posting: Mapping[str, Any],
    average_doc_len: float,
    idf: float,
    *,
    k1: float,
    b: float,
) -> float:
    if average_doc_len <= 0:
        return 0.0
    tf = posting["tf"]
    normalization = 1.0 - b + b * (posting["doc_len"] / average_doc_len)
    value = idf * ((tf * (k1 + 1.0)) / (tf + k1 * normalization))
    return round_score(value)


def _finalize(
    accumulator: Mapping[str, list[dict[str, Any]]],
    *,
    merge_policy: str,
    top_k: int,
    detail_key: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for doc_id in sorted(accumulator):
        details = sorted(
            accumulator[doc_id],
            key=lambda item: (
                item.get("target_lang", ""),
                item.get("target_term", item.get("term", "")),
                tuple(item.get("rule_ids", ())),
                item.get("source", {}).get("line", 0),
            ),
        )
        contributions = [detail["contribution"] for detail in details]
        if merge_policy == "sum":
            final_score = round_score(math.fsum(contributions))
        else:
            final_score = round_score(max(contributions, default=0.0))
        results.append(
            {
                "doc_id": doc_id,
                "score": final_score,
                detail_key: final_score,
                "recomputed_score": round_score(
                    math.fsum(contributions)
                    if merge_policy == "sum"
                    else max(contributions, default=0.0)
                ),
                "merge_policy": merge_policy,
                "contributions": details,
            }
        )
    results.sort(key=lambda item: (-item["score"], item["doc_id"]))
    return results[:top_k]


def _rank_of(doc_id: str, results: Sequence[Mapping[str, Any]]) -> int | None:
    for index, result in enumerate(results, start=1):
        if result["doc_id"] == doc_id:
            return index
    return None


def retrieval_bytes(document: Mapping[str, Any]) -> bytes:
    return stable_json_bytes(document)
