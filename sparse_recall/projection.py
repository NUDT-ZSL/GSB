"""Fixed-dimension sparse projection and sparse dot products."""

from __future__ import annotations

import math
from typing import Any, Sequence

from .canonical import DEFAULT_DECIMALS, DEFAULT_SUBNORMAL_FLOOR, stable_float
from .models import Document, Query
from .weights import WeightTable


def project_terms(
    terms: dict[str, float],
    weights: WeightTable,
    *,
    decimals: int = DEFAULT_DECIMALS,
    subnormal_floor: float = DEFAULT_SUBNORMAL_FLOOR,
) -> list[list[float | int]]:
    contributions: dict[int, list[float]] = {}
    for term in sorted(terms):
        entries = weights.terms.get(term)
        if entries is None:
            raise KeyError(f"unknown term {term!r}")
        frequency = float(terms[term])
        for dim, weight in entries:
            contribution = frequency * weight
            if abs(contribution) >= subnormal_floor:
                contributions.setdefault(dim, []).append(contribution)

    dims: list[list[float | int]] = []
    for dim in sorted(contributions):
        value = stable_float(
            math.fsum(contributions[dim]),
            decimals=decimals,
            subnormal_floor=subnormal_floor,
        )
        if value != 0.0:
            dims.append([dim, value])
    return dims


def project_document(
    document: Document,
    weights: WeightTable,
    *,
    decimals: int = DEFAULT_DECIMALS,
    subnormal_floor: float = DEFAULT_SUBNORMAL_FLOOR,
) -> dict[str, Any]:
    dims = project_terms(
        document.terms,
        weights,
        decimals=decimals,
        subnormal_floor=subnormal_floor,
    )
    return {
        "kind": "doc",
        "batch_no": document.batch_no,
        "doc_id": document.doc_id,
        "dimension": weights.dimension,
        "nnz": len(dims),
        "dims": dims,
    }


def project_query(
    query: Query,
    weights: WeightTable,
    *,
    decimals: int = DEFAULT_DECIMALS,
    subnormal_floor: float = DEFAULT_SUBNORMAL_FLOOR,
) -> dict[str, Any]:
    dims = project_terms(
        query.terms,
        weights,
        decimals=decimals,
        subnormal_floor=subnormal_floor,
    )
    return {
        "kind": "query",
        "batch_no": query.batch_no,
        "query_id": query.query_id,
        "dimension": weights.dimension,
        "nnz": len(dims),
        "dims": dims,
    }


def sparse_dot(
    left: Sequence[Sequence[float | int]],
    right: Sequence[Sequence[float | int]],
    *,
    decimals: int = DEFAULT_DECIMALS,
    subnormal_floor: float = DEFAULT_SUBNORMAL_FLOOR,
) -> float:
    left_index = 0
    right_index = 0
    products: list[float] = []
    while left_index < len(left) and right_index < len(right):
        left_dim = int(left[left_index][0])
        right_dim = int(right[right_index][0])
        if left_dim == right_dim:
            products.append(float(left[left_index][1]) * float(right[right_index][1]))
            left_index += 1
            right_index += 1
        elif left_dim < right_dim:
            left_index += 1
        else:
            right_index += 1
    return stable_float(
        math.fsum(products),
        decimals=decimals,
        subnormal_floor=subnormal_floor,
    )


def project_document_chunk(
    documents: Sequence[Document],
    weights: WeightTable,
    *,
    chunk_index: int,
    decimals: int = DEFAULT_DECIMALS,
    subnormal_floor: float = DEFAULT_SUBNORMAL_FLOOR,
) -> dict[str, Any]:
    vectors = [
        project_document(
            document,
            weights,
            decimals=decimals,
            subnormal_floor=subnormal_floor,
        )
        for document in sorted(documents, key=lambda item: item.doc_id)
    ]
    return {
        "batch_no": int(documents[0].batch_no) if documents else None,
        "chunk_index": chunk_index,
        "vectors": vectors,
    }


def merge_document_chunks(chunks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for chunk in sorted(chunks, key=lambda item: int(item["chunk_index"])):
        merged.extend(chunk["vectors"])
    merged.sort(key=lambda item: (int(item["batch_no"]), str(item["doc_id"])))
    return merged
