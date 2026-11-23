"""Deterministic local sample generation (no network, fixed seeds)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .config import FusionConfig
from .models import Document, Query

TERM_POOL = [
    "anchor", "apple", "apply", "beam", "bean", "cache", "cabin", "data",
    "date", "delta", "engine", "entry", "fusion", "filter", "graph", "grain",
    "index", "input", "join", "joint", "kernel", "knob", "lambda", "layer",
    "model", "merge", "normal", "node", "output", "order", "prefix", "proxy",
    "query", "queue", "rank", "recall", "score", "shard", "term", "token",
    "union", "update", "vector", "vertex", "weight", "window",
]
CATEGORIES = ["news", "shop", "tech", "video"]
VECTOR_DIM = 8


def _stable_rng(seed: int, namespace: int) -> np.random.Generator:
    # Child streams are derived deterministically from the master seed.
    ss = np.random.SeedSequence([seed, namespace])
    return np.random.default_rng(ss)


def generate_documents(n_docs: int, seed: int) -> list[Document]:
    rng = _stable_rng(seed, 101)
    docs: list[Document] = []
    for index in range(n_docs):
        doc_id = f"doc_{index:05d}"
        term_count = int(rng.integers(3, 9))
        chosen = rng.choice(TERM_POOL, size=term_count, replace=False)
        terms = {
            str(term): int(rng.integers(1, 8)) for term in sorted(chosen)
        }
        doc_len = sum(terms.values())
        category = CATEGORIES[int(rng.integers(0, len(CATEGORIES)))]
        quality = round(float(rng.uniform(0.1, 1.0)), 6)
        vector = np.round(rng.normal(0.0, 1.0, VECTOR_DIM), 6)
        docs.append(
            Document(
                doc_id=doc_id,
                terms=terms,
                doc_len=doc_len,
                category=category,
                quality_score=quality,
                vector=tuple(float(x) for x in vector),
            )
        )
    return docs


def generate_queries(n_queries: int, seed: int) -> list[Query]:
    rng = _stable_rng(seed, 202)
    queries: list[Query] = []
    for index in range(n_queries):
        query_id = f"q_{index:04d}"
        term_count = int(rng.integers(1, 4))
        chosen = rng.choice(TERM_POOL, size=term_count, replace=False)
        terms = tuple(sorted(str(term) for term in chosen))
        top_k = int(rng.integers(5, 15))
        category_filter = None
        min_quality = None
        mode = int(rng.integers(0, 4))
        if mode == 1:
            category_filter = CATEGORIES[int(rng.integers(0, len(CATEGORIES)))]
        elif mode == 2:
            min_quality = round(float(rng.uniform(0.2, 0.8)), 4)
        elif mode == 3:
            category_filter = CATEGORIES[int(rng.integers(0, len(CATEGORIES)))]
            min_quality = round(float(rng.uniform(0.2, 0.8)), 4)
        vector = np.round(rng.normal(0.0, 1.0, VECTOR_DIM), 6)
        queries.append(
            Query(
                query_id=query_id,
                terms=terms,
                top_k=top_k,
                category_filter=category_filter,
                min_quality=min_quality,
                vector=tuple(float(x) for x in vector),
            )
        )
    return queries


def write_jsonl(path: str | Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
            handle.write("\n")


def read_jsonl(path: str | Path) -> list[dict]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def generate_dataset(
    out_dir: str | Path,
    n_docs: int = 120,
    n_queries: int = 25,
    seed: int = 20261124,
) -> dict[str, Path]:
    out_dir = Path(out_dir)
    docs = generate_documents(n_docs, seed)
    queries = generate_queries(n_queries, seed)
    doc_path = out_dir / "documents.jsonl"
    query_path = out_dir / "queries.jsonl"
    write_jsonl(doc_path, [doc.to_jsonl() for doc in docs])
    write_jsonl(query_path, [query.to_jsonl() for query in queries])
    return {"documents": doc_path, "queries": query_path}


def load_dataset(data_dir: str | Path) -> tuple[list[Document], list[Query]]:
    data_dir = Path(data_dir)
    docs = [Document.from_jsonl(row) for row in read_jsonl(data_dir / "documents.jsonl")]
    queries = [Query.from_jsonl(row) for row in read_jsonl(data_dir / "queries.jsonl")]
    return docs, queries


def write_default_config(path: str | Path, overrides: dict | None = None) -> Path:
    cfg = FusionConfig.default(overrides)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg.save(path)
    return path
