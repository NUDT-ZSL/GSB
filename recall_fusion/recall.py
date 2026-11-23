"""Four deterministic recall channels and the shared in-memory index."""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from .models import Candidate, Document, Query


class RecallIndex:
    """Immutable, pre-built index shared by every worker (fork-safe on POSIX)."""

    def __init__(self, documents: list[Document]):
        self.documents: dict[str, Document] = {}
        self.inverted: dict[str, dict[str, int]] = defaultdict(dict)
        self.prefix_index: dict[str, dict[str, int]] = defaultdict(dict)
        self.category_docs: dict[str, list[str]] = defaultdict(list)
        vectors: list[np.ndarray] = []
        doc_ids: list[str] = []

        for doc in documents:
            self.documents[doc.doc_id] = doc
            doc_ids.append(doc.doc_id)
            vectors.append(np.asarray(doc.vector, dtype=np.float64))
            self.category_docs[doc.category].append(doc.doc_id)
            for term, tf in doc.terms.items():
                self.inverted[term][doc.doc_id] = tf
                for size in range(1, len(term) + 1):
                    self.prefix_index[term[:size]][doc.doc_id] = (
                        self.prefix_index[term[:size]].get(doc.doc_id, 0) + tf
                    )

        self.doc_ids = tuple(sorted(self.documents))
        matrix = np.vstack(vectors) if vectors else np.zeros((0, 1))
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        self.vector_matrix = matrix / norms
        self.vector_doc_ids = tuple(doc_ids)

    def passes_filters(self, doc: Document, query: Query) -> bool:
        if query.category_filter is not None and doc.category != query.category_filter:
            return False
        if query.min_quality is not None and doc.quality_score < query.min_quality:
            return False
        return True

    def _truncate(
        self, scored: list[tuple[float, str]], limit: int
    ) -> list[Candidate]:
        # raw score desc, then doc_id asc for deterministic ties.
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            Candidate(doc_id=doc_id, channel=self._current_channel, raw_score=score)
            for score, doc_id in scored[:limit]
        ]

    def inverted_recall(self, query: Query, limit: int) -> list[Candidate]:
        self._current_channel = "inverted"
        accum: dict[str, float] = defaultdict(float)
        for term in query.terms:
            for doc_id, tf in self.inverted.get(term, {}).items():
                doc = self.documents[doc_id]
                if not self.passes_filters(doc, query):
                    continue
                accum[doc_id] += tf / doc.doc_len
        return self._truncate([(score, doc_id) for doc_id, score in accum.items()], limit)

    def prefix_recall(self, query: Query, limit: int) -> list[Candidate]:
        self._current_channel = "prefix"
        accum: dict[str, float] = defaultdict(float)
        for term in query.terms:
            for size in range(1, len(term) + 1):
                prefix = term[:size]
                for doc_id, weight in self.prefix_index.get(prefix, {}).items():
                    doc = self.documents[doc_id]
                    if not self.passes_filters(doc, query):
                        continue
                    # Longer prefixes are more specific and earn higher weight.
                    accum[doc_id] += weight * size / len(term)
        return self._truncate([(score, doc_id) for doc_id, score in accum.items()], limit)

    def category_recall(self, query: Query, limit: int) -> list[Candidate]:
        self._current_channel = "category"
        if query.category_filter is None:
            return []
        scored: list[tuple[float, str]] = []
        for doc_id in self.category_docs.get(query.category_filter, []):
            doc = self.documents[doc_id]
            if not self.passes_filters(doc, query):
                continue
            scored.append((doc.quality_score, doc_id))
        return self._truncate(scored, limit)

    def vector_recall(self, query: Query, limit: int) -> list[Candidate]:
        self._current_channel = "vector"
        if not query.vector or self.vector_matrix.size == 0:
            return []
        qvec = np.asarray(query.vector, dtype=np.float64)
        qnorm = np.linalg.norm(qvec)
        if qnorm == 0.0:
            return []
        qvec = qvec / qnorm
        sims = self.vector_matrix @ qvec
        eligible: list[tuple[float, str]] = []
        for doc_id, sim in zip(self.vector_doc_ids, sims):
            doc = self.documents[doc_id]
            if not self.passes_filters(doc, query):
                continue
            eligible.append((float(sim), doc_id))
        return self._truncate(eligible, limit)

    def run_all(
        self, query: Query, caps: dict[str, int]
    ) -> dict[str, list[Candidate]]:
        return {
            "inverted": self.inverted_recall(query, caps["inverted"]),
            "prefix": self.prefix_recall(query, caps["prefix"]),
            "category": self.category_recall(query, caps["category"]),
            "vector": self.vector_recall(query, caps["vector"]),
        }
