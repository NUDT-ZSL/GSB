"""Core immutable data structures for documents, queries, candidates and results."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


CHANNELS = ("inverted", "prefix", "category", "vector")


@dataclass(frozen=True)
class Document:
    doc_id: str
    terms: dict[str, int]
    doc_len: int
    category: str
    quality_score: float
    vector: tuple[float, ...]

    def to_jsonl(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "terms": dict(self.terms),
            "doc_len": self.doc_len,
            "category": self.category,
            "quality_score": self.quality_score,
            "vector": list(self.vector),
        }

    @staticmethod
    def from_jsonl(row: dict[str, Any]) -> "Document":
        return Document(
            doc_id=str(row["doc_id"]),
            terms={str(t): int(tf) for t, tf in row["terms"].items()},
            doc_len=int(row["doc_len"]),
            category=str(row["category"]),
            quality_score=float(row["quality_score"]),
            vector=tuple(float(x) for x in row["vector"]),
        )


@dataclass(frozen=True)
class Query:
    query_id: str
    terms: tuple[str, ...]
    top_k: int
    category_filter: str | None = None
    min_quality: float | None = None
    vector: tuple[float, ...] = ()

    def to_jsonl(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "query_id": self.query_id,
            "terms": list(self.terms),
            "top_k": self.top_k,
        }
        if self.category_filter is not None:
            row["category_filter"] = self.category_filter
        if self.min_quality is not None:
            row["min_quality"] = self.min_quality
        if self.vector:
            row["vector"] = list(self.vector)
        return row

    @staticmethod
    def from_jsonl(row: dict[str, Any]) -> "Query":
        filters = row.get("filters") or {}
        return Query(
            query_id=str(row["query_id"]),
            terms=tuple(str(t) for t in row["terms"]),
            top_k=int(row["top_k"]),
            category_filter=filters.get("category") or row.get("category_filter"),
            min_quality=(
                float(filters["min_quality"])
                if "min_quality" in filters
                else (float(row["min_quality"]) if "min_quality" in row else None)
            ),
            vector=tuple(float(x) for x in row.get("vector", ())),
        )


@dataclass(frozen=True)
class Candidate:
    """A single channel's raw recall hit."""

    doc_id: str
    channel: str
    raw_score: float


@dataclass(frozen=True)
class ChannelHit:
    channel: str
    raw_score: float


@dataclass
class MergedCandidate:
    """Doc-level merge of all channel hits, including the kept hit used by fusion."""

    doc_id: str
    hits: list[ChannelHit]
    kept_channel: str
    kept_raw_score: float

    @property
    def hit_channels(self) -> list[str]:
        return [hit.channel for hit in self.hits]


@dataclass
class FusionDetail:
    channel: str
    raw_score: float
    normalized_score: float
    weight: float
    weighted_contribution: float


@dataclass
class FusedCandidate:
    doc_id: str
    final_score: float
    kept_channel: str
    details: list[FusionDetail]
    all_hits: list[ChannelHit] = field(default_factory=list)

    def recompute_final_score(self) -> float:
        return sum(detail.weighted_contribution for detail in self.details)


@dataclass
class QueryResult:
    query_id: str
    batch_id: int
    channel_candidates: dict[str, list[Candidate]]
    merged: list[MergedCandidate]
    fused: list[FusedCandidate]
    per_channel_caps: dict[str, int]
    cost_recall: int
    cost_fusion: int
    two_stage: bool

    def top_doc_ids(self) -> list[str]:
        return [candidate.doc_id for candidate in self.fused]

    def compact_for_eviction(self) -> "QueryResult":
        """Drop bulky per-candidate arrays but keep counts and the fused trace."""
        return replace(self, channel_candidates={}, merged=[])
