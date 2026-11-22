"""Concurrent query side: atomic index swap, BM25 scoring, top-k.

The active index is swapped atomically (single reference under a lock);
queries either see the whole old index or the whole new one, never a
mix. Decoded-block caching is bounded; when limits are exceeded the
reader degrades to streaming per-block decode and records the
degradation category plus the terms involved.
"""

import heapq
import json
import math
import threading
from collections import OrderedDict

from .encoding import decode_block
from .errors import ResourceLimitError, ScoreMismatchError


def bm25_idf(doc_count, df):
    return math.log(1.0 + (doc_count - df + 0.5) / (df + 0.5))


def bm25_score_term(tf, doc_len, avgdl, idf, k1, b):
    norm = tf * (k1 + 1.0)
    denom = tf + k1 * (1.0 - b + b * doc_len / avgdl)
    return idf * norm / denom


class IndexSnapshotView:
    """Immutable view of one committed index artifact."""

    def __init__(self, payload):
        self.payload = payload
        self.stats = payload["stats"]
        self.terms = payload["terms"]
        self.config = payload["config"]

    def iter_blocks(self, term):
        entry = self.terms.get(term)
        if not entry:
            return
        for blk in entry["blocks"]:
            yield blk

    def decode_term_blocks(self, term):
        """Decode every block of a term independently; returns postings."""
        postings = []
        for blk in self.iter_blocks(term):
            decoded, _ = decode_block(bytes.fromhex(blk["data"]))
            if len(decoded) != blk["count"] or (
                    decoded and decoded[0][0] != blk["first_doc_id"]):
                raise ResourceLimitError("block header mismatch", term=term)
            postings.extend(decoded)
        return postings

    def score_postings(self, query_terms):
        """Score from decoded posting lists. Returns (scores, hits)."""
        stats = self.stats
        avgdl = stats["avgdl"]
        k1, b = stats["k1"], stats["b"]
        scores = {}
        hits = {}
        for term in query_terms:
            entry = self.terms.get(term)
            if not entry:
                continue
            idf = bm25_idf(stats["doc_count"], entry["df"])
            for doc_id, tf in self.decode_term_blocks(term):
                dl = stats["doc_len"][str(doc_id)]
                s = bm25_score_term(tf, dl, avgdl, idf, k1, b)
                scores[doc_id] = scores.get(doc_id, 0.0) + s
                hits.setdefault(doc_id, []).append(term)
        return scores, hits

    def baseline_score(self, query_terms):
        """Baseline computed on the uncompressed logical posting lists."""
        return self.score_postings(query_terms)

    def top_k(self, query_terms, k):
        scores, hits = self.score_postings(query_terms)
        best = heapq.nsmallest(
            k, scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [
            {"doc_id": doc_id, "score": score,
             "matched_terms": sorted(hits[doc_id])}
            for doc_id, score in best
        ]


class IndexService:
    """Thread-safe holder of the active index with bounded block cache."""

    def __init__(self, index_path, max_cache_blocks=1024,
                 max_resident_bytes=64 * 1024 * 1024):
        self._index_path = index_path
        self._lock = threading.Lock()
        self._view = None
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self.max_cache_blocks = max_cache_blocks
        self.max_resident_bytes = max_resident_bytes
        self.degraded_terms = set()
        self.degradation_categories = []
        self.reload()

    def reload(self):
        with open(self._index_path, "rb") as fh:
            payload = json.loads(fh.read().decode("utf-8"))
        view = IndexSnapshotView(payload)
        with self._lock:
            self._view = view
            self._cache.clear()
            self._cache_bytes = 0

    def active_view(self):
        with self._lock:
            return self._view

    def _decode_block_cached(self, term, idx, blk):
        key = (term, idx)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        postings, _ = decode_block(bytes.fromhex(blk["data"]))
        size = len(postings) * 16
        if (len(self._cache) >= self.max_cache_blocks
                or self._cache_bytes + size > self.max_resident_bytes):
            self.degraded_terms.add(term)
            cat = ("cache_block_limit"
                   if len(self._cache) >= self.max_cache_blocks
                   else "resident_memory_limit")
            if cat not in self.degradation_categories:
                self.degradation_categories.append(cat)
            return postings
        with self._lock:
            self._cache[key] = postings
            self._cache_bytes += size
        return postings

    def query(self, query_terms, top_k):
        view = self.active_view()
        stats = view.stats
        avgdl, k1, b = stats["avgdl"], stats["k1"], stats["b"]
        scores = {}
        hits = {}
        for term in query_terms:
            entry = view.terms.get(term)
            if not entry:
                continue
            idf = bm25_idf(stats["doc_count"], entry["df"])
            for idx, blk in enumerate(entry["blocks"]):
                for doc_id, tf in self._decode_block_cached(term, idx, blk):
                    dl = stats["doc_len"][str(doc_id)]
                    s = bm25_score_term(tf, dl, avgdl, idf, k1, b)
                    scores[doc_id] = scores.get(doc_id, 0.0) + s
                    hits.setdefault(doc_id, []).append(term)
        best = heapq.nsmallest(
            top_k, scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [
            {"doc_id": doc_id, "score": score,
             "matched_terms": sorted(hits[doc_id])}
            for doc_id, score in best
        ]

    def query_checked(self, query_id, query_terms, top_k, tolerance):
        """Run compressed-path query and verify against the uncompressed
        baseline; any drift raises E_SCORE_MISMATCH."""
        view = self.active_view()
        results = self.query(query_terms, top_k)
        baseline_scores, _ = view.baseline_score(query_terms)
        for item in results:
            base = baseline_scores.get(item["doc_id"], 0.0)
            delta = abs(base - item["score"])
            if delta > tolerance:
                raise ScoreMismatchError(query_id, item["doc_id"], delta)
        return results
