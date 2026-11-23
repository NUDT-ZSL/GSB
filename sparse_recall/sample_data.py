"""Deterministic local document/query batch and weight-table generator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import canonical_json_line, sha256_file, write_bytes_atomic, write_json_atomic
from .weights import generate_term_weight_table, save_weight_table


GENERATOR_VERSION = "sparse-recall.sample.v1"


def _batch_sizes(total: int, batches: int) -> list[int]:
    if batches < 1:
        raise ValueError("batches must be >= 1")
    base = total // batches
    remainder = total % batches
    return [base + (1 if index < remainder else 0) for index in range(batches)]


def generate_dataset(
    out_dir: str | Path,
    *,
    seed: int = 20261124,
    doc_count: int = 72,
    doc_batches: int = 6,
    query_count: int = 30,
    query_batches: int = 3,
    vocabulary_size: int = 48,
    dimension: int = 64,
    nnz_per_term: int = 3,
    min_doc_terms: int = 4,
    max_doc_terms: int = 9,
    min_query_terms: int = 2,
    max_query_terms: int = 6,
) -> dict[str, Any]:
    if doc_count < 1 or query_count < 1 or vocabulary_size < max_doc_terms:
        raise ValueError("invalid generator sizes")
    if min_doc_terms < 1 or max_doc_terms > vocabulary_size:
        raise ValueError("invalid document term bounds")
    if min_query_terms < 1 or max_query_terms > vocabulary_size:
        raise ValueError("invalid query term bounds")

    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    vocabulary = [f"t{index:03d}" for index in range(vocabulary_size)]
    weight_table = generate_term_weight_table(
        vocabulary,
        dimension=dimension,
        nnz_per_term=nnz_per_term,
        seed=seed,
    )

    rng = np.random.default_rng(np.random.SeedSequence([seed, 1001]))
    sizes = _batch_sizes(doc_count, doc_batches)
    documents: list[dict[str, Any]] = []
    doc_index = 0
    for batch_index, size in enumerate(sizes, start=1):
        for _ in range(size):
            term_count = int(rng.integers(min_doc_terms, max_doc_terms + 1))
            positions = np.sort(rng.choice(vocabulary_size, size=term_count, replace=False))
            terms: dict[str, float] = {}
            for position in positions.tolist():
                frequency = int(rng.integers(1, 5))
                terms[vocabulary[position]] = float(frequency)
            doc_len = int(sum(terms.values()))
            documents.append(
                {
                    "batch_no": batch_index,
                    "doc_id": f"doc-{doc_index:05d}",
                    "doc_len": doc_len,
                    "terms": {term: terms[term] for term in sorted(terms)},
                }
            )
            doc_index += 1

    query_sizes = _batch_sizes(query_count, query_batches)
    queries: list[dict[str, Any]] = []
    query_index = 0
    for batch_index, size in enumerate(query_sizes):
        batch_no = 1001 + batch_index
        priority = query_batches - batch_index
        for _ in range(size):
            term_count = int(rng.integers(min_query_terms, max_query_terms + 1))
            positions = np.sort(rng.choice(vocabulary_size, size=term_count, replace=False))
            terms = {}
            for position in positions.tolist():
                frequency = int(rng.integers(1, 4))
                terms[vocabulary[position]] = float(frequency)
            queries.append(
                {
                    "batch_no": batch_no,
                    "priority": priority,
                    "query_id": f"qry-{query_index:05d}",
                    "terms": {term: terms[term] for term in sorted(terms)},
                    "top_k": int(rng.integers(3, 9)),
                }
            )
            query_index += 1

    documents.sort(key=lambda row: (row["batch_no"], row["doc_id"]))
    queries.sort(key=lambda row: (row["batch_no"], row["query_id"]))

    docs_path = output / "documents.jsonl"
    queries_path = output / "queries.jsonl"
    weights_path = output / "term_weights.json"
    write_bytes_atomic(
        docs_path,
        b"".join(canonical_json_line(row) for row in documents),
    )
    write_bytes_atomic(
        queries_path,
        b"".join(canonical_json_line(row) for row in queries),
    )
    save_weight_table(str(weights_path), weight_table)

    manifest = {
        "schema": GENERATOR_VERSION,
        "seed": seed,
        "parameters": {
            "doc_count": doc_count,
            "doc_batches": doc_batches,
            "query_count": query_count,
            "query_batches": query_batches,
            "vocabulary_size": vocabulary_size,
            "dimension": dimension,
            "nnz_per_term": nnz_per_term,
            "min_doc_terms": min_doc_terms,
            "max_doc_terms": max_doc_terms,
            "min_query_terms": min_query_terms,
            "max_query_terms": max_query_terms,
        },
        "files": {
            "documents.jsonl": {
                "sha256": sha256_file(docs_path),
                "rows": len(documents),
            },
            "queries.jsonl": {
                "sha256": sha256_file(queries_path),
                "rows": len(queries),
            },
            "term_weights.json": {
                "sha256": sha256_file(weights_path),
                "content_hash": weight_table.content_hash,
            },
        },
    }
    write_json_atomic(output / "dataset_manifest.json", manifest)
    return manifest


def materialize_dataset_json(out_dir: str | Path, manifest: dict[str, Any] | None = None) -> str:
    """Return a compact canonical summary, useful for smoke checks."""

    output = Path(out_dir)
    if manifest is None:
        manifest = json.loads((output / "dataset_manifest.json").read_text(encoding="utf-8"))
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
