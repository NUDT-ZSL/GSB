"""Deterministic sample document / query JSONL generation (NumPy-based)."""

import json
import os

import numpy as np

DEFAULT_VOCAB = [f"term{i:03d}" for i in range(60)]


def generate_docs(path, num_docs=200, vocab=None, seed=42,
                  min_terms=3, max_terms=12):
    vocab = vocab or DEFAULT_VOCAB
    rng = np.random.default_rng(seed)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for doc_id in range(1, num_docs + 1):
            n_terms = int(rng.integers(min_terms, max_terms + 1))
            idx = rng.choice(len(vocab), size=n_terms, replace=False)
            idx.sort()
            terms = []
            doc_len = 0
            for i in idx:
                tf = int(rng.integers(1, 6))
                terms.append({"term": vocab[int(i)], "tf": tf})
                doc_len += tf
            rec = {"doc_id": doc_id, "terms": terms, "doc_len": doc_len}
            fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def generate_queries(path, num_queries=20, vocab=None, seed=123,
                     min_terms=1, max_terms=4, top_k=10):
    vocab = vocab or DEFAULT_VOCAB
    rng = np.random.default_rng(seed)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for query_id in range(1, num_queries + 1):
            n_terms = int(rng.integers(min_terms, max_terms + 1))
            idx = rng.choice(len(vocab), size=n_terms, replace=False)
            idx.sort()
            terms = [vocab[int(i)] for i in idx]
            rec = {"query_id": query_id, "terms": terms, "top_k": top_k}
            fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
    return path
