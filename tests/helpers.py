from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from index_diag.util import canonical_json


def make_config(
    *,
    generation: int = 1,
    chunk_size: int = 4,
    compression: str = "delta_zlib",
    delta_step: int = 2,
    vocabulary: list[str] | None = None,
    max_batch_docs: int = 100,
    max_postings_bytes: int = 1_048_576,
    max_comparisons: int = 100_000,
    degrade_policy: str = "error",
) -> dict[str, Any]:
    return {
        "chunk_size": chunk_size,
        "compression": compression,
        "delta_step": delta_step,
        "generation": generation,
        "logical_clock": 100 + generation,
        "resource_limits": {
            "degrade_policy": degrade_policy,
            "max_batch_docs": max_batch_docs,
            "max_comparisons": max_comparisons,
            "max_postings_bytes": max_postings_bytes,
        },
        "version": 1,
        "vocabulary": vocabulary or ["a", "b", "c", "d"],
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def document(
    batch_no: int,
    doc_id: int,
    terms: list[str],
    *,
    include_positions: bool = True,
) -> dict[str, Any]:
    frequencies = Counter(terms)
    positions: dict[str, list[int]] = {}
    for index, term in enumerate(terms):
        positions.setdefault(term, []).append(index)
    body: dict[str, Any] = {
        "batch_no": batch_no,
        "content_hash": "content-" + str(doc_id),
        "doc_id": doc_id,
        "doc_length": len(terms),
        "term_frequencies": dict(sorted(frequencies.items())),
        "terms": terms,
    }
    if include_positions:
        body["positions"] = dict(sorted(positions.items()))
    return body


def write_jsonl(path: Path, documents: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(canonical_json(document) + "\n" for document in documents),
        encoding="utf-8",
    )
    return path
