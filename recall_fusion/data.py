"""Deterministic local sample generation and in-process document indexes.

All randomness derives from the fixed configuration seed. Python's ``random``
drives term/category structure while NumPy ``default_rng`` drives dense
vectors; per-entity vector seeds are derived from a stable MD5 hash so that a
document/query vector is reproducible in any worker process without shipping
RNG state.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import DataGenConfig, PRECISION_FP32


def _stable_seed(base_seed: int, identifier: str, tag: str) -> int:
    digest = hashlib.md5(f"{base_seed}|{tag}|{identifier}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def vector_for(identifier: str, dim: int, base_seed: int, dtype: str) -> np.ndarray:
    """Reproducible dense vector for a doc/query id, independent of process."""
    rng = np.random.default_rng(_stable_seed(base_seed, identifier, "vector"))
    vector = rng.standard_normal(dim)
    return vector.astype(np.float32 if dtype == PRECISION_FP32 else np.float64)


@dataclass(frozen=True)
class Document:
    doc_id: str
    terms: dict[str, int]
    doc_len: int
    category: str
    quality_score: float


@dataclass(frozen=True)
class Query:
    query_id: str
    terms: list[str]
    top_k: int
    filters: dict[str, object]


def generate_dataset(cfg: DataGenConfig) -> tuple[list[Document], list[Query]]:
    rng = random.Random(cfg.seed)
    vocab = [f"term_{idx:03d}" for idx in range(cfg.num_terms)]
    cats = list(cfg.categories)

    docs: list[Document] = []
    for doc_idx in range(cfg.num_docs):
        doc_id = f"d{doc_idx:06d}"
        distinct = rng.randint(5, 12)
        chosen = rng.sample(vocab, distinct)
        terms = {term: rng.randint(1, 5) for term in chosen}
        doc_len = sum(terms.values())
        category = cats[rng.randrange(len(cats))]
        quality = round(rng.random(), 6)
        docs.append(Document(doc_id, terms, doc_len, category, quality))

    queries: list[Query] = []
    for query_idx in range(cfg.num_queries):
        query_id = f"q{query_idx:05d}"
        n_terms = rng.randint(2, 5)
        terms = rng.sample(vocab, n_terms)
        top_k = rng.choice((5, 10, 15, 20))
        filters: dict[str, object] = {}
        # A deterministic mix of filter shapes across the query stream.
        if query_idx % 4 == 1:
            filters["category"] = cats[query_idx % len(cats)]
        if query_idx % 5 == 2:
            filters["min_quality"] = round(0.2 + (query_idx % 5) * 0.12, 4)
        queries.append(Query(query_id, terms, top_k, filters))
    return docs, queries


def write_jsonl(rows: list[object], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            if hasattr(row, "__dataclass_fields__"):
                payload = {
                    key: getattr(row, key) for key in row.__dataclass_fields__
                }
            else:
                payload = row
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False))
            handle.write("\n")


def _passes_filters(doc_idx: int, store: "DataStore", filters: MappingLike) -> bool:
    category = filters.get("category")
    if category is not None and store.categories[doc_idx] != category:
        return False
    min_quality = filters.get("min_quality")
    if min_quality is not None and store.quality[doc_idx] < min_quality:
        return False
    return True


MappingLike = dict


class DataStore:
    """Picklable read-only index shipped to worker processes once per pool."""

    def __init__(self, docs: list[Document], cfg: DataGenConfig, precision: str):
        self.cfg = cfg
        self.precision = precision
        self.doc_ids = [doc.doc_id for doc in docs]
        self.doc_id_to_idx = {doc_id: idx for idx, doc_id in enumerate(self.doc_ids)}
        self.doc_terms = [dict(doc.terms) for doc in docs]
        self.doc_len = np.asarray([doc.doc_len for doc in docs], dtype=np.int64)
        self.categories = [doc.category for doc in docs]
        self.quality = np.asarray(
            [doc.quality_score for doc in docs], dtype=np.float64
        )

        self.postings: dict[str, list[tuple[int, int]]] = {}
        for idx, doc in enumerate(docs):
            for term, tf in doc.terms.items():
                self.postings.setdefault(term, []).append((idx, tf))
        for postings in self.postings.values():
            postings.sort(key=lambda pair: pair[0])

        self.vocab = sorted(self.postings.keys())

        self.category_docs: dict[str, list[int]] = {}
        for idx, doc in enumerate(docs):
            self.category_docs.setdefault(doc.category, []).append(idx)
        for cat, idxs in self.category_docs.items():
            idxs.sort(key=lambda i: (-float(self.quality[i]), self.doc_ids[i]))
        self.global_quality = sorted(
            range(len(docs)), key=lambda i: (-float(self.quality[i]), self.doc_ids[i])
        )

        vectors = [
            vector_for(doc.doc_id, cfg.vector_dim, cfg.seed, precision) for doc in docs
        ]
        self.vectors = np.vstack(vectors)
        norms = np.linalg.norm(self.vectors, axis=1, keepdims=True)
        self.vector_norms = np.where(norms == 0.0, 1.0, norms)

    @property
    def num_docs(self) -> int:
        return len(self.doc_ids)

    def prefix_matching_terms(self, prefix: str) -> list[str]:
        start = bisect.bisect_left(self.vocab, prefix)
        end = bisect.bisect_right(self.vocab, prefix + "\uffff")
        return self.vocab[start:end]

    def passes_filters(self, doc_idx: int, filters: dict[str, object]) -> bool:
        return _passes_filters(doc_idx, self, filters)

    def query_vector(self, query_id: str) -> np.ndarray:
        vector = vector_for(
            query_id, self.cfg.vector_dim, self.cfg.seed, self.precision
        )
        return vector / max(float(np.linalg.norm(vector)), 1e-12)
