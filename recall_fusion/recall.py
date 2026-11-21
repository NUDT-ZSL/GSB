"""Four deterministic recall channels.

Every channel orders ties by ascending ``doc_id`` (ids are zero padded, so
lexicographic order matches index order). Given identical inputs, config and
precision flag the candidate lists are byte-for-byte reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import (
    CHANNEL_CATEGORY,
    CHANNEL_INVERTED,
    CHANNEL_PREFIX,
    CHANNEL_VECTOR,
)
from .data import DataStore, Query


@dataclass(frozen=True)
class Candidate:
    doc_id: str
    raw_score: float
    rank: int


@dataclass
class ChannelOutput:
    channel: str
    requested_limit: int
    candidates: list[Candidate]
    truncated: bool
    matched_total: int
    empty_reason: str = ""  # "", "no_hits" or "limit_zero"

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "requested_limit": self.requested_limit,
            "truncated": self.truncated,
            "matched_total": self.matched_total,
            "empty_reason": self.empty_reason,
            "candidates": [
                {"doc_id": c.doc_id, "raw_score": c.raw_score, "rank": c.rank}
                for c in self.candidates
            ],
        }


def _finalize(
    channel: str,
    limit: int,
    scored: list[tuple[int, float]],
    store: DataStore,
) -> ChannelOutput:
    if limit == 0:
        return ChannelOutput(channel, 0, [], False, len(scored), "limit_zero")
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    matched_total = len(scored)
    chosen = scored[:limit]
    candidates = [
        Candidate(store.doc_ids[idx], float(score), rank + 1)
        for rank, (idx, score) in enumerate(chosen)
    ]
    return ChannelOutput(
        channel=channel,
        requested_limit=limit,
        candidates=candidates,
        truncated=matched_total > len(chosen),
        matched_total=matched_total,
        empty_reason="" if candidates else "no_hits",
    )


def inverted_recall(
    store: DataStore, query: Query, limit: int
) -> ChannelOutput:
    accum: dict[int, float] = {}
    for term in query.terms:
        for doc_idx, tf in store.postings.get(term, ()):
            accum[doc_idx] = accum.get(doc_idx, 0.0) + tf
    scored = [
        (idx, score)
        for idx, score in accum.items()
        if store.passes_filters(idx, query.filters)
    ]
    return _finalize(CHANNEL_INVERTED, limit, scored, store)


def prefix_recall(store: DataStore, query: Query, limit: int) -> ChannelOutput:
    accum: dict[int, float] = {}
    for prefix in query.terms:
        for term in store.prefix_matching_terms(prefix):
            for doc_idx, tf in store.postings.get(term, ()):
                accum[doc_idx] = accum.get(doc_idx, 0.0) + tf
    scored = [
        (idx, score)
        for idx, score in accum.items()
        if store.passes_filters(idx, query.filters)
    ]
    return _finalize(CHANNEL_PREFIX, limit, scored, store)


def category_recall(
    store: DataStore, query: Query, limit: int
) -> ChannelOutput:
    category = query.filters.get("category")
    if category is not None:
        pool = list(store.category_docs.get(str(category), ()))
    else:
        pool = list(store.global_quality)
    scored = [
        (idx, float(store.quality[idx]))
        for idx in pool
        if store.passes_filters(idx, query.filters)
    ]
    return _finalize(CHANNEL_CATEGORY, limit, scored, store)


def vector_recall(store: DataStore, query: Query, limit: int) -> ChannelOutput:
    query_vector = store.query_vector(query.query_id)
    doc_vectors = store.vectors / store.vector_norms
    sims = doc_vectors @ query_vector
    scored = [
        (int(idx), float(sims[idx]))
        for idx in range(store.num_docs)
        if store.passes_filters(idx, query.filters)
    ]
    return _finalize(CHANNEL_VECTOR, limit, scored, store)


CHANNEL_FUNCS = {
    CHANNEL_INVERTED: inverted_recall,
    CHANNEL_PREFIX: prefix_recall,
    CHANNEL_CATEGORY: category_recall,
    CHANNEL_VECTOR: vector_recall,
}


def run_channel(
    channel: str, store: DataStore, query: Query, limit: int
) -> ChannelOutput:
    return CHANNEL_FUNCS[channel](store, query, limit)
