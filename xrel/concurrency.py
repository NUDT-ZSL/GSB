"""Non-blocking commit gate and incremental invalidation planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Set

from .errors import StageBusy
from .model import BridgeResource, Query


class StageLock:
    """A try-only lock: contention is reported immediately, never awaited."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self._held = False

    def try_acquire(self) -> None:
        if self._held:
            raise StageBusy(self.stage)
        self._held = True

    def release(self) -> None:
        self._held = False

    def __enter__(self) -> "StageLock":
        self.try_acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


@dataclass
class ChangeSet:
    """Describes one incremental resource change."""

    kind: str  # bridge_entry | direction | document_batch
    # Bridge entry changes.
    source_lang: str = ""
    source_terms: List[str] = field(default_factory=list)
    target_langs: List[str] = field(default_factory=list)
    # Document batch changes.
    doc_langs: List[str] = field(default_factory=list)


@dataclass
class InvalidationPlan:
    affected_partitions: List[str]
    affected_queries: List[str]
    affected_terms: List[str]
    recompute_queries: List[str]
    reuse_queries: List[str]

    def to_dict(self) -> dict:
        return {
            "affected_partitions": list(self.affected_partitions),
            "affected_queries": list(self.affected_queries),
            "affected_terms": list(self.affected_terms),
            "recompute_queries": list(self.recompute_queries),
            "reuse_queries": list(self.reuse_queries),
        }


def _queries_touching_terms(queries: List[Query], terms: Set[str]) -> Set[str]:
    return {
        query.query_id
        for query in queries
        if any(term in terms for term in query.terms)
    }


def plan_invalidation(
    change: ChangeSet,
    bridge: BridgeResource,
    queries: List[Query],
    cached_query_ids: Set[str],
) -> InvalidationPlan:
    """Derive affected partitions and queries for an incremental change.

    The expansion/scoring graph per query has three dependency levels:
      * bridge entries  -> expansion candidates -> scores -> top-k;
      * directions      -> expansion feasibility -> scores -> top-k;
      * partition stats -> scores -> top-k.
    Only queries whose dependency suffix changed are recomputed; every other
    cached upstream artifact is reused.
    """

    affected_partitions: Set[str] = set()
    affected_terms: Set[str] = set()
    affected_queries: Set[str] = set()

    if change.kind == "bridge_entry":
        affected_partitions.update(change.target_langs)
        affected_terms.update(change.source_terms)
        # Queries in the source language using the corrected source terms.
        affected_queries |= {
            query.query_id
            for query in queries
            if query.query_lang == change.source_lang
            and any(term in change.source_terms for term in query.terms)
        }

    elif change.kind == "direction":
        # A direction add/remove changes every query using that source language.
        affected_partitions.update(change.target_langs)
        source_lang = change.source_lang
        affected_queries |= {
            query.query_id for query in queries if query.query_lang == source_lang
        }
        affected_terms.update(
            term
            for query in queries
            if query.query_lang == source_lang
            for term in query.terms
        )

    elif change.kind == "document_batch":
        # New documents change partition statistics for their languages.
        affected_partitions.update(change.doc_langs)
        # Any query whose expansion reaches an affected partition is impacted.
        for query in queries:
            reaches = set()
            for term in query.terms:
                for target_lang, _, _ in bridge.lookup(query.query_lang, term):
                    reaches.add(target_lang)
            if reaches & set(change.doc_langs):
                affected_queries.add(query.query_id)
                affected_terms.add(term)

    else:
        raise ValueError(f"unknown change kind: {change.kind}")

    recompute = affected_queries & set(cached_query_ids)
    reuse = set(cached_query_ids) - recompute
    return InvalidationPlan(
        affected_partitions=sorted(affected_partitions),
        affected_queries=sorted(affected_queries),
        affected_terms=sorted(affected_terms),
        recompute_queries=sorted(recompute),
        reuse_queries=sorted(reuse),
    )
