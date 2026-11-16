"""交互查询执行：解码倒排块、合取检索、位置摘要与延迟预算观测。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .codec import block_key, decode_block
from .errors import (
    E_BUDGET_EXHAUSTED,
    E_DECODE_FAILED,
    E_POSITION_UNAVAILABLE,
    E_QUERY_FAILED,
    make_failure,
)
from .normalization import normalize_text, tokenize
from .store import IndexLayout, load_json, read_index_shard


@dataclass
class QueryRequest:
    query_id: str
    terms: list[str]
    sort_key: int
    want_positions: bool = True
    expected_count: int | None = None


@dataclass
class QueryResponse:
    query_id: str
    sort_key: int
    terms: list[str]
    doc_ids: list[str] = field(default_factory=list)
    hits: list[dict[str, Any]] = field(default_factory=list)
    decoded_blocks: int = 0
    decoded_terms: int = 0
    filtered_terms: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    budget_ms: int = 0
    budget_exhausted: bool = False
    intermediate_state: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def canonical(self) -> dict[str, Any]:
        """一致性比对用的规范化结果（剔除耗时等非确定字段）。"""
        return {
            "query_id": self.query_id,
            "sort_key": self.sort_key,
            "terms": self.terms,
            "doc_ids": self.doc_ids,
            "hits": self.hits,
            "decoded_blocks": self.decoded_blocks,
            "decoded_terms": self.decoded_terms,
            "filtered_terms": self.filtered_terms,
            "budget_exhausted": self.budget_exhausted,
            "failures": self.failures,
            "intermediate_state": self.intermediate_state,
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.canonical()
        data["elapsed_ms"] = round(self.elapsed_ms, 6)
        data["budget_ms"] = self.budget_ms
        return data


class QueryEngine:
    """按当前清单解码块并执行合取查询，块按分片惰性加载。"""

    def __init__(self, layout: IndexLayout, budget_ms: int | None = None):
        self.layout = layout
        self.manifest = load_json(layout.manifest_path)
        self._shard_cache: dict[int, dict[str, bytes]] = {}
        self._block_index: dict[str, dict[str, Any]] = {
            entry["block_key"]: entry for entry in self.manifest["blocks"]
        }
        self.budget_ms = budget_ms or 0
        self.store_positions = bool(self.manifest["store_positions"])
        self.filtered_terms = set(self.manifest.get("filtered_terms", []))
        self.level = self.manifest["compression_level"]
        self.doc_ids = self.manifest["doc_ids"]

    def version_fingerprint(self) -> str:
        return self.manifest["version_fingerprint"]

    def _load_shard(self, shard_id: int) -> dict[str, bytes]:
        if shard_id not in self._shard_cache:
            self._shard_cache[shard_id] = read_index_shard(
                self.layout.index_shard_path(shard_id)
            )
        return self._shard_cache[shard_id]

    def _decode_term(self, term: str) -> tuple[list[tuple[int, tuple[int, ...]]], int]:
        entries = [
            entry for entry in self.manifest["blocks"] if entry["term"] == term
        ]
        postings: list[tuple[int, tuple[int, ...]]] = []
        decoded_blocks = 0
        for entry in sorted(entries, key=lambda item: item["chunk_index"]):
            shard = self._load_shard(entry["shard_id"])
            blob = shard[entry["block_key"]]
            decoded_term, chunk_index, level, has_pos, decoded = decode_block(blob)
            if level != self.level or has_pos != self.store_positions:
                raise RuntimeError(
                    f"块 {entry['block_key']} 格式与清单声明不一致"
                )
            for posting in decoded:
                postings.append((posting.doc_seq, posting.positions))
            decoded_blocks += 1
        postings.sort(key=lambda item: item[0])
        return postings, decoded_blocks

    def run(self, request: QueryRequest) -> QueryResponse:
        start = time.monotonic_ns()
        response = QueryResponse(
            query_id=request.query_id,
            sort_key=request.sort_key,
            terms=request.terms,
            budget_ms=self.budget_ms,
        )
        normalized_terms = self._normalize_terms(request, response)
        if response.failures:
            response.elapsed_ms = self._elapsed(start)
            return response

        candidate_seqs: set[int] | None = None
        position_map: dict[int, dict[str, list[int]]] = {}
        term_candidate_counts: dict[str, int] = {}

        for term in normalized_terms:
            if term in self.filtered_terms:
                response.filtered_terms.append(term)
                candidate_seqs = set()
                term_candidate_counts[term] = 0
                continue
            try:
                postings, block_count = self._decode_term(term)
            except Exception as exc:
                response.failures.append(
                    make_failure(
                        f"{request.query_id}:{term}",
                        "decode",
                        E_DECODE_FAILED,
                        exc,
                    ).to_dict()
                )
                continue
            response.decoded_blocks += block_count
            response.decoded_terms += 1
            doc_seqs = {doc_seq for doc_seq, _ in postings}
            term_candidate_counts[term] = len(doc_seqs)
            if self.store_positions:
                for doc_seq, positions in postings:
                    position_map.setdefault(doc_seq, {})[term] = list(positions)
            if candidate_seqs is None:
                candidate_seqs = set(doc_seqs)
            else:
                candidate_seqs &= doc_seqs
            response.intermediate_state[f"after_{term}"] = (
                len(candidate_seqs) if candidate_seqs is not None else 0
            )
            self._mark_budget(response, start)

        if response.failures:
            response.elapsed_ms = self._elapsed(start)
            return response

        final_seqs = sorted(candidate_seqs or set())
        response.doc_ids = [self.doc_ids[seq] for seq in final_seqs]
        response.hits = self._build_hits(
            request, normalized_terms, final_seqs, position_map, response
        )
        response.intermediate_state["final_candidate_count"] = len(final_seqs)
        response.intermediate_state["term_candidate_counts"] = term_candidate_counts
        response.elapsed_ms = self._elapsed(start)
        self._mark_budget(response, start)
        return response

    def _normalize_terms(
        self,
        request: QueryRequest,
        response: QueryResponse,
    ) -> list[str]:
        try:
            normalized: list[str] = []
            for term in request.terms:
                tokens = tokenize(term)
                if len(tokens) != 1:
                    raise ValueError(f"查询词项必须归一化为单词: {term!r}")
                normalized.append(tokens[0])
            if not normalized:
                raise ValueError("查询词项为空")
            if len(set(normalized)) != len(normalized):
                raise ValueError("查询词项归一化后重复")
            return normalized
        except Exception as exc:
            response.failures.append(
                make_failure(request.query_id, "parse", E_QUERY_FAILED, exc).to_dict()
            )
            return []

    def _build_hits(
        self,
        request: QueryRequest,
        terms: list[str],
        doc_seqs: list[int],
        position_map: dict[int, dict[str, list[int]]],
        response: QueryResponse,
    ) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        position_unavailable_reported = False
        for doc_seq in doc_seqs:
            doc_id = self.doc_ids[doc_seq]
            hit: dict[str, Any] = {
                "doc_id": doc_id,
                "matched_terms": list(terms),
            }
            if request.want_positions:
                if not self.store_positions:
                    if not position_unavailable_reported:
                        response.failures.append(
                            make_failure(
                                request.query_id,
                                "query",
                                E_POSITION_UNAVAILABLE,
                                "位置信息保留关闭，无法返回位置摘要",
                            ).to_dict()
                        )
                        position_unavailable_reported = True
                    hit["position_summary"] = None
                else:
                    hit["position_summary"] = {
                        term: position_map.get(doc_seq, {}).get(term, [])
                        for term in terms
                    }
            hits.append(hit)
        return hits

    @staticmethod
    def _elapsed(start_ns: int) -> float:
        return (time.monotonic_ns() - start_ns) / 1_000_000.0

    def _mark_budget(self, response: QueryResponse, start_ns: int) -> None:
        if self.budget_ms >= 0 and self._elapsed(start_ns) > self.budget_ms:
            response.budget_exhausted = True
            response.intermediate_state["budget_code"] = E_BUDGET_EXHAUSTED
            response.intermediate_state["budget_ms"] = self.budget_ms
